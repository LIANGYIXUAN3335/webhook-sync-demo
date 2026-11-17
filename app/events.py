"""Event store: durable, deduplicated record of every verified Stripe event."""

from __future__ import annotations

import time
from dataclasses import dataclass

from app import db

HANDLED_TYPES = {"checkout.session.completed"}


@dataclass
class DeliveryResult:
    event_id: str
    duplicate: bool
    delivery_count: int
    status: str


def extract_refs(event: dict) -> tuple[str | None, str | None]:
    """(checkout_session_id, order_id) from a checkout.session.* event."""
    obj = (event.get("data") or {}).get("object") or {}
    if obj.get("object") != "checkout.session":
        return None, None
    metadata = obj.get("metadata") or {}
    order_id = metadata.get("order_id") or obj.get("client_reference_id")
    return obj.get("id"), order_id


def record_delivery(path: str, event: dict, raw_payload: str, max_attempts: int) -> DeliveryResult:
    """Persist a verified event, keyed by its Stripe event ID.

    First delivery inserts the row (queued for the worker if it is a handled type).
    Any later delivery of the same ID only bumps `delivery_count`; it never re-queues
    the event or touches its sync state, so duplicates cannot cause a second update.
    """
    now = time.time()
    event_type = event.get("type", "")
    session_id, order_id = extract_refs(event)
    status = "pending" if event_type in HANDLED_TYPES else "ignored"
    next_attempt_at = now if status == "pending" else None
    completed_at = now if status == "ignored" else None
    last_error = None if status == "pending" else f"event type {event_type!r} is not synced"

    with db.transaction(path) as conn:
        row = conn.execute(
            """
            INSERT INTO events (id, type, livemode, payload, checkout_session_id, order_id,
                                status, max_attempts, next_attempt_at, last_error,
                                received_at, last_delivery_at, updated_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                delivery_count   = delivery_count + 1,
                last_delivery_at = excluded.last_delivery_at
            RETURNING delivery_count, status
            """,
            (
                event["id"], event_type, int(bool(event.get("livemode"))), raw_payload,
                session_id, order_id, status, max_attempts, next_attempt_at, last_error,
                now, now, now, completed_at,
            ),
        ).fetchone()
    return DeliveryResult(
        event_id=event["id"],
        duplicate=row["delivery_count"] > 1,
        delivery_count=row["delivery_count"],
        status=row["status"],
    )


def get_event(path: str, event_id: str):
    with db.session(path) as conn:
        return conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()


def list_events(path: str, status: str | None = None, limit: int = 200):
    with db.session(path) as conn:
        if status:
            return conn.execute(
                "SELECT * FROM events WHERE status = ? ORDER BY received_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM events ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()


def list_attempts(path: str, event_id: str):
    with db.session(path) as conn:
        return conn.execute(
            "SELECT * FROM event_attempts WHERE event_id = ? ORDER BY id", (event_id,)
        ).fetchall()


def status_counts(path: str) -> dict[str, int]:
    with db.session(path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM events GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
