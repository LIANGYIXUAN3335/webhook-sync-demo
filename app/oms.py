"""Mock order management service (OMS) — the downstream system the worker syncs to.

It is mounted inside the demo app at /mock-oms so the whole demo runs offline, but the
worker only talks to it over HTTP (ORDER_API_BASE_URL), exactly as it would talk to a
real external service. Two properties matter for the demo:

* Idempotency: every request carries an `Idempotency-Key` (derived from the Checkout
  Session ID). A key that was already applied returns the stored response instead of
  updating the order again, and an order that is already paid is never re-paid.
* Fault injection: the dashboard can queue faults (503, 429, timeout, ...) that the
  next requests consume, to demonstrate retries and failure handling.
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import db

router = APIRouter(prefix="/mock-oms", tags=["mock order service"])

FAULT_MODES = {
    "503": "503 Service Unavailable (transient)",
    "500": "500 Internal Server Error (transient)",
    "429": "429 Too Many Requests + Retry-After: 2 (transient)",
    "timeout": "Stall past the client timeout, nothing applied (transient)",
    "timeout_after_commit": "Apply the update, then stall past the client timeout",
    "400": "400 Bad Request (permanent)",
}


# --- fault queue -------------------------------------------------------------------

def push_faults(path: str, mode: str, count: int = 1) -> None:
    if mode not in FAULT_MODES:
        raise ValueError(f"unknown fault mode {mode!r}")
    now = time.time()
    with db.transaction(path) as conn:
        for _ in range(max(1, min(count, 20))):
            conn.execute("INSERT INTO oms_faults (mode, created_at) VALUES (?, ?)", (mode, now))


def pop_fault(path: str) -> str | None:
    with db.transaction(path) as conn:
        row = conn.execute("SELECT id, mode FROM oms_faults ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM oms_faults WHERE id = ?", (row["id"],))
        return row["mode"]


def list_faults(path: str) -> list[str]:
    with db.session(path) as conn:
        return [r["mode"] for r in conn.execute("SELECT mode FROM oms_faults ORDER BY id")]


def clear_faults(path: str) -> None:
    with db.session(path) as conn:
        conn.execute("DELETE FROM oms_faults")


# --- orders --------------------------------------------------------------------------

def list_orders(path: str):
    with db.session(path) as conn:
        return conn.execute("SELECT * FROM orders ORDER BY id").fetchall()


def get_order(path: str, order_id: str):
    with db.session(path) as conn:
        return conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def apply_payment(path: str, order_id: str, idempotency_key: str | None, body: dict
                  ) -> tuple[int, dict, bool]:
    """Mark an order paid. Returns (status_code, response_body, idempotent_replay)."""
    session_id = body.get("checkout_session_id")
    if not idempotency_key:
        return 400, {"error": "Idempotency-Key header is required"}, False
    if not session_id:
        return 400, {"error": "checkout_session_id is required"}, False

    now = time.time()
    with db.transaction(path) as conn:
        seen = conn.execute(
            "SELECT * FROM oms_idempotency WHERE key = ?", (idempotency_key,)
        ).fetchone()
        if seen is not None:
            if seen["order_id"] != order_id:
                return 422, {"error": "Idempotency-Key was already used for a different order"}, False
            return seen["status_code"], json.loads(seen["response_json"]), True

        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if order is None:
            return 404, {"error": f"order {order_id} not found"}, False
        if order["status"] == "paid":
            if order["paid_session_id"] == session_id:
                return 200, {"order_id": order_id, "status": "paid", "applied": False,
                             "note": "already paid by this checkout session"}, True
            return 409, {"error": f"order {order_id} already paid by {order['paid_session_id']}"}, False

        # Conditional update: only a pending order can transition to paid.
        cur = conn.execute(
            "UPDATE orders SET status = 'paid', paid_session_id = ?, paid_amount_cents = ?,"
            " paid_at = ?, update_count = update_count + 1 WHERE id = ? AND status = 'pending'",
            (session_id, body.get("amount_total"), now, order_id),
        )
        if cur.rowcount != 1:  # unreachable under BEGIN IMMEDIATE, but never double-apply
            return 409, {"error": f"order {order_id} changed concurrently"}, False
        response = {"order_id": order_id, "status": "paid", "applied": True,
                    "checkout_session_id": session_id}
        conn.execute(
            "INSERT INTO oms_idempotency (key, order_id, status_code, response_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (idempotency_key, order_id, 200, json.dumps(response), now),
        )
    return 200, response, False


# --- HTTP routes ---------------------------------------------------------------------

@router.post("/orders/{order_id}/payments")
async def mark_paid(
    order_id: str,
    request: Request,
    idempotency_key: str | None = Header(default=None),
) -> JSONResponse:
    settings = request.app.state.settings
    path = settings.database_path
    body = await request.json()

    fault = await run_in_threadpool(pop_fault, path)
    if fault in {"503", "500"}:
        return JSONResponse({"error": f"injected fault {fault}"}, status_code=int(fault))
    if fault == "429":
        return JSONResponse({"error": "injected fault 429"}, status_code=429,
                            headers={"Retry-After": "2"})
    if fault == "400":
        return JSONResponse({"error": "injected fault 400: malformed request"}, status_code=400)
    if fault == "timeout":
        await asyncio.sleep(settings.mock_oms_stall_seconds)
        return JSONResponse({"error": "injected timeout"}, status_code=504)

    status, payload, replayed = await run_in_threadpool(
        apply_payment, path, order_id, idempotency_key, body
    )
    if fault == "timeout_after_commit":
        # The update is committed, but the caller gives up before seeing the response.
        await asyncio.sleep(settings.mock_oms_stall_seconds)
    headers = {"Idempotent-Replayed": "true"} if replayed else {}
    return JSONResponse(payload, status_code=status, headers=headers)


@router.get("/orders")
def orders(request: Request) -> list[dict]:
    return [dict(r) for r in list_orders(request.app.state.settings.database_path)]
