"""Synthetic Stripe event builder and signer for the offline demo path and tests.

The payload mirrors the shape of a real `checkout.session.completed` snapshot event
(only the fields this app reads, plus a few for realism). All values are fake.
The signature follows Stripe's documented scheme so that the receiver can verify it
with the official `stripe.Webhook.construct_event`, exactly as for real deliveries:

    Stripe-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time


def _rand(prefix: str, n: int = 24) -> str:
    return f"{prefix}_{secrets.token_hex(n // 2)}"


def build_checkout_completed_event(
    order_id: str | None,
    amount_total: int = 2500,
    currency: str = "usd",
    payment_status: str = "paid",
    event_id: str | None = None,
    session_id: str | None = None,
    event_type: str = "checkout.session.completed",
) -> dict:
    session_id = session_id or _rand("cs_test_sim", 40)
    metadata = {"order_id": order_id} if order_id else {}
    return {
        "id": event_id or _rand("evt_sim"),
        "object": "event",
        "api_version": "2025-06-30.basil",
        "created": int(time.time()),
        "livemode": False,
        "pending_webhooks": 1,
        "type": event_type,
        "data": {
            "object": {
                "id": session_id,
                "object": "checkout.session",
                "mode": "payment",
                "status": "complete",
                "payment_status": payment_status,
                "amount_total": amount_total,
                "currency": currency,
                "client_reference_id": order_id,
                "metadata": metadata,
                "customer_details": {"email": "synthetic.buyer@example.test", "name": "Synthetic Buyer"},
                "payment_intent": _rand("pi_sim"),
                "livemode": False,
            }
        },
    }


def encode(event: dict) -> str:
    return json.dumps(event, separators=(",", ":"))


def sign(payload: str, secret: str, timestamp: int | None = None) -> str:
    """Return a Stripe-Signature header value for `payload`."""
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"
