"""Admin dashboard (server-rendered Jinja templates) plus the offline simulator.

The simulator builds synthetic `checkout.session.completed` events, signs them with the
configured STRIPE_WEBHOOK_SECRET, and delivers them to POST /webhooks/stripe through the
app's own ASGI stack — so they pass through exactly the same signature verification,
persistence, and deduplication as real Stripe deliveries.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, events, oms, synthetic

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ["pending", "processing", "retrying", "succeeded", "failed", "ignored"]


# --- template helpers ----------------------------------------------------------------

def _fmt_time(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if ts else "—"


def _fmt_relative(ts: float | None) -> str:
    if not ts:
        return "—"
    delta = ts - time.time()
    if delta <= 0:
        return "due now"
    return f"in {delta:.0f}s" if delta >= 1 else "in <1s"


def _fmt_money(cents: int | None, currency: str = "usd") -> str:
    if cents is None:
        return "—"
    return f"{cents / 100:,.2f} {currency.upper()}"


templates.env.filters["time"] = _fmt_time
templates.env.filters["relative"] = _fmt_relative
templates.env.filters["money"] = _fmt_money


def _settings(request: Request):
    return request.app.state.settings


def _redirect(request: Request, msg: str, level: str = "info", to: str | None = None) -> RedirectResponse:
    target = to or request.headers.get("referer") or "/"
    target = target.split("?msg=")[0].split("&msg=")[0]
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}{urlencode({'msg': msg, 'level': level})}", status_code=303)


def _live_context(request: Request, status: str | None) -> dict:
    s = _settings(request)
    path = s.database_path
    return {
        "request": request,
        "settings": s,
        "status_filter": status,
        "statuses": STATUSES,
        "counts": events.status_counts(path),
        "events": events.list_events(path, status),
        "orders": oms.list_orders(path),
        "faults": oms.list_faults(path),
        "fault_modes": oms.FAULT_MODES,
    }


def _require_simulator(request: Request) -> None:
    if not _settings(request).enable_simulator:
        raise HTTPException(404, "simulator disabled (ENABLE_SIMULATOR=false)")


# --- pages ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, status: str | None = None, msg: str | None = None,
              level: str = "info"):
    if status not in STATUSES:
        status = None
    ctx = _live_context(request, status)
    ctx.update(msg=msg, level=level)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/live", response_class=HTMLResponse)
def live_partial(request: Request, status: str | None = None):
    """Auto-refresh fragment: counts, events table, orders, fault queue."""
    if status not in STATUSES:
        status = None
    return templates.TemplateResponse(request, "_live_partial.html", _live_context(request, status))


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str, msg: str | None = None, level: str = "info"):
    path = _settings(request).database_path
    row = events.get_event(path, event_id)
    if row is None:
        raise HTTPException(404, "event not found")
    payload = json.dumps(json.loads(row["payload"]), indent=2)
    order = oms.get_order(path, row["order_id"]) if row["order_id"] else None
    return templates.TemplateResponse(request, "event_detail.html", {
        "request": request, "event": row, "payload": payload, "order": order,
        "attempts": events.list_attempts(path, event_id),
        "settings": _settings(request), "msg": msg, "level": level,
    })


# --- actions -------------------------------------------------------------------------

@router.post("/events/{event_id}/replay")
def replay(request: Request, event_id: str):
    s = _settings(request)
    if events.replay_event(s.database_path, event_id, s.max_attempts):
        return _redirect(request, f"Replay queued for {event_id}", "ok")
    return _redirect(request, f"{event_id} is not in 'failed' state; nothing replayed", "warn")


@router.get("/api/events")
def api_events(request: Request, status: str | None = None) -> JSONResponse:
    rows = events.list_events(_settings(request).database_path, status)
    return JSONResponse([{k: r[k] for k in r.keys() if k != "payload"} for r in rows])


# --- offline simulator ---------------------------------------------------------------

async def _deliver(request: Request, payload: str, signature: str) -> httpx.Response:
    """POST to our own webhook endpoint through the ASGI app (no network involved)."""
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://simulator.local") as c:
        return await c.post("/webhooks/stripe", content=payload, headers={
            "Content-Type": "application/json", "Stripe-Signature": signature,
            "User-Agent": "webhook-sync-demo-simulator",
        })


def _describe(resp: httpx.Response) -> tuple[str, str]:
    body = resp.json()
    if resp.status_code != 200:
        return f"Webhook endpoint answered {resp.status_code}: {body.get('error')}", "warn"
    if body.get("duplicate"):
        return f"Delivered {body['event_id']} again → 200, deduplicated (no new work queued)", "ok"
    return f"Delivered {body['event_id']} → 200, stored as '{body['status']}'", "ok"


@router.post("/simulate/checkout")
async def simulate_checkout(
    request: Request,
    order_id: str = Form(...),
    payment_status: str = Form("paid"),
    signature: str = Form("valid"),
):
    _require_simulator(request)
    s = _settings(request)
    order = await run_in_threadpool(oms.get_order, s.database_path, order_id) if order_id else None
    event = synthetic.build_checkout_completed_event(
        order_id=None if order_id == "__none__" else order_id,
        amount_total=order["amount_cents"] if order else 1000,
        currency=order["currency"] if order else "usd",
        payment_status=payment_status,
    )
    payload = synthetic.encode(event)
    secret = s.stripe_webhook_secret if signature == "valid" else "whsec_attacker_guess"
    resp = await _deliver(request, payload, synthetic.sign(payload, secret))
    msg, level = _describe(resp)
    return _redirect(request, msg, level, to="/")


@router.post("/simulate/redeliver/{event_id}")
async def simulate_redeliver(request: Request, event_id: str):
    """Re-send a stored event's exact raw body with a fresh signature, like Stripe's
    automatic retries or `stripe events resend`."""
    _require_simulator(request)
    s = _settings(request)
    row = await run_in_threadpool(events.get_event, s.database_path, event_id)
    if row is None:
        raise HTTPException(404, "event not found")
    resp = await _deliver(request, row["payload"], synthetic.sign(row["payload"], s.stripe_webhook_secret))
    msg, level = _describe(resp)
    return _redirect(request, msg, level)


@router.post("/faults")
def add_fault(request: Request, mode: str = Form(...), count: int = Form(1)):
    _require_simulator(request)
    try:
        oms.push_faults(_settings(request).database_path, mode, count)
    except ValueError as exc:
        return _redirect(request, str(exc), "warn")
    return _redirect(request, f"Queued {count} × fault '{mode}' on the mock order service", "ok")


@router.post("/faults/clear")
def clear_faults(request: Request):
    _require_simulator(request)
    oms.clear_faults(_settings(request).database_path)
    return _redirect(request, "Fault queue cleared", "ok")


@router.post("/demo/reset")
def reset(request: Request):
    _require_simulator(request)
    db.reset_demo_data(_settings(request).database_path)
    return _redirect(request, "Demo data reset: events cleared, synthetic orders back to pending", "ok", to="/")
