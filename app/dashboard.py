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
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import db, events, oms, synthetic

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

STATUSES = ["pending", "processing", "retrying", "succeeded", "failed", "ignored"]
SIMULATED_TYPES = [
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.expired",
]


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
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency.lower())
    return f"{symbol}{cents / 100:,.2f}" if symbol else f"{cents / 100:,.2f} {currency.upper()}"


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    return f"{seconds * 1000:.0f} ms" if seconds < 1 else f"{seconds:.1f} s"


templates.env.filters["duration"] = _fmt_duration
templates.env.filters["time"] = _fmt_time
templates.env.filters["relative"] = _fmt_relative
templates.env.filters["money"] = _fmt_money


def _settings(request: Request):
    return request.app.state.settings


def _redirect(request: Request, msg: str, level: str = "info", to: str | None = None) -> RedirectResponse:
    """Redirect back (POST/redirect/GET) with a flash message.

    Only the path and query of the Referer are used, so the redirect always stays on
    this site even if the header points elsewhere (no open redirect).
    """
    parts = urlsplit(to or request.headers.get("referer") or "/")
    path = parts.path if parts.path.startswith("/") and not parts.path.startswith("//") else "/"
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"msg", "level"}]
    query += [("msg", msg), ("level", level)]
    return RedirectResponse(f"{path}?{urlencode(query)}", status_code=303)


def _live_context(request: Request, status: str | None, q: str | None = None) -> dict:
    s = _settings(request)
    path = s.database_path
    return {
        "request": request,
        "settings": s,
        "status_filter": status,
        "q": q or "",
        "statuses": STATUSES,
        "counts": events.status_counts(path),
        "events": events.list_events(path, status, q),
        "orders": oms.list_orders(path),
        "faults": oms.list_faults(path),
        "fault_modes": oms.FAULT_MODES,
        "simulated_types": SIMULATED_TYPES,
    }


def _require_simulator(request: Request) -> None:
    if not _settings(request).enable_simulator:
        raise HTTPException(404, "simulator disabled (ENABLE_SIMULATOR=false)")


# --- pages ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    msg: str | None = None,
    level: str = "info",
):
    if status not in STATUSES:
        status = None
    ctx = _live_context(request, status, (q or "").strip() or None)
    ctx.update(msg=msg, level=level)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/partials/live", response_class=HTMLResponse)
def live_partial(request: Request, status: str | None = None, q: str | None = None):
    """Auto-refresh fragment: counts, events table, orders, fault queue."""
    if status not in STATUSES:
        status = None
    return templates.TemplateResponse(
        request, "_live_partial.html", _live_context(request, status, (q or "").strip() or None)
    )


@router.get("/events/{event_id}", response_class=HTMLResponse)
def event_detail(request: Request, event_id: str, msg: str | None = None, level: str = "info"):
    path = _settings(request).database_path
    row = events.get_event(path, event_id)
    if row is None:
        raise HTTPException(404, "event not found")
    payload = json.dumps(json.loads(row["payload"]), indent=2)
    order = oms.get_order(path, row["order_id"]) if row["order_id"] else None
    attempts = events.list_attempts(path, event_id)
    timeline, prev_finished = [], None
    for a in attempts:
        timeline.append(
            {
                "a": a,
                "duration": (a["finished_at"] - a["started_at"]) if a["finished_at"] else None,
                "waited": (a["started_at"] - prev_finished) if prev_finished else None,
            }
        )
        prev_finished = a["finished_at"] or prev_finished
    return templates.TemplateResponse(
        request,
        "event_detail.html",
        {
            "request": request,
            "event": row,
            "payload": payload,
            "order": order,
            "timeline": timeline,
            "idempotency_key": (
                f"checkout_session:{row['checkout_session_id']}" if row["checkout_session_id"] else None
            ),
            "terminal": row["status"] in {"succeeded", "failed", "ignored"},
            "settings": _settings(request),
            "msg": msg,
            "level": level,
        },
    )


# --- actions -------------------------------------------------------------------------


@router.post("/events/{event_id}/replay")
def replay(request: Request, event_id: str):
    s = _settings(request)
    if events.replay_event(s.database_path, event_id, s.max_attempts):
        return _redirect(request, f"Replay queued for {event_id}", "ok")
    return _redirect(request, f"{event_id} is not in 'failed' state; nothing replayed", "warn")


@router.post("/events/replay-failed")
def replay_failed(request: Request):
    s = _settings(request)
    n = events.replay_all_failed(s.database_path, s.max_attempts)
    return _redirect(
        request,
        f"Replay queued for {n} failed event(s)" if n else "No failed events to replay",
        "ok" if n else "info",
    )


@router.get("/api/info")
def api_info(request: Request) -> dict:
    """Non-secret runtime settings (used by scripts/demo_all.py)."""
    s = _settings(request)
    return {
        "max_attempts": s.max_attempts,
        "retry_base_seconds": s.retry_base_seconds,
        "retry_max_seconds": s.retry_max_seconds,
        "order_api_timeout_seconds": s.order_api_timeout_seconds,
        "simulator_enabled": s.enable_simulator,
        "worker_enabled": s.worker_enabled,
    }


@router.get("/api/events")
def api_events(request: Request, status: str | None = None, q: str | None = None) -> JSONResponse:
    rows = events.list_events(_settings(request).database_path, status, q)
    # sqlite3.Row iterates over values, not keys, so .keys() is required here.
    return JSONResponse([{k: r[k] for k in r.keys() if k != "payload"} for r in rows])  # noqa: SIM118


# --- offline simulator ---------------------------------------------------------------


async def _deliver(request: Request, payload: str, signature: str) -> httpx.Response:
    """POST to our own webhook endpoint through the ASGI app (no network involved)."""
    transport = httpx.ASGITransport(app=request.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://simulator.local") as c:
        return await c.post(
            "/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": signature,
                "User-Agent": "webhook-sync-demo-simulator",
            },
        )


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
    event_type: str = Form("checkout.session.completed"),
):
    _require_simulator(request)
    s = _settings(request)
    order = await run_in_threadpool(oms.get_order, s.database_path, order_id) if order_id else None
    event = synthetic.build_checkout_completed_event(
        order_id=None if order_id == "__none__" else order_id,
        amount_total=order["amount_cents"] if order else 1000,
        currency=order["currency"] if order else "usd",
        payment_status=payment_status,
        event_type=event_type if event_type in SIMULATED_TYPES else "checkout.session.completed",
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


@router.post("/simulate/async-succeeded/{event_id}")
async def simulate_async_succeeded(request: Request, event_id: str):
    """Follow an unpaid checkout.session.completed with the async_payment_succeeded event
    Stripe sends once a delayed payment method settles (same Checkout Session)."""
    _require_simulator(request)
    s = _settings(request)
    row = await run_in_threadpool(events.get_event, s.database_path, event_id)
    if row is None or not row["checkout_session_id"]:
        raise HTTPException(404, "checkout session event not found")
    session = json.loads(row["payload"])["data"]["object"]
    event = synthetic.build_checkout_completed_event(
        order_id=row["order_id"],
        amount_total=session.get("amount_total") or 0,
        currency=session.get("currency") or "usd",
        payment_status="paid",
        session_id=row["checkout_session_id"],
        event_type="checkout.session.async_payment_succeeded",
    )
    payload = synthetic.encode(event)
    resp = await _deliver(request, payload, synthetic.sign(payload, s.stripe_webhook_secret))
    msg, level = _describe(resp)
    return _redirect(request, msg, level, to="/")


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
    return _redirect(
        request, "Demo data reset: events cleared, synthetic orders back to pending", "ok", to="/"
    )
