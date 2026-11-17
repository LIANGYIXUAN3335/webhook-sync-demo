"""Stripe webhook receiver.

Contract: verify the signature over the *raw* body, durably store the event, and only
then return 2xx. All slow or failure-prone work (calling the order service) happens
later in the background worker, so Stripe never waits on it and never sees our
downstream errors.
"""

from __future__ import annotations

import json
import logging

import stripe
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app import events

log = logging.getLogger("webhook_sync.webhooks")
router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    if not settings.stripe_webhook_secret:
        # Fail closed: without a secret nothing can be verified. Non-2xx makes Stripe retry.
        return JSONResponse({"error": "STRIPE_WEBHOOK_SECRET is not configured"}, status_code=500)

    payload = await request.body()  # raw bytes, before any JSON parsing
    sig_header = request.headers.get("stripe-signature")
    try:
        stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except stripe.SignatureVerificationError as exc:
        log.warning("rejected webhook: invalid signature (%s)", exc.user_message or exc)
        return JSONResponse({"error": "invalid signature"}, status_code=400)
    except ValueError:
        return JSONResponse({"error": "invalid payload"}, status_code=400)

    raw = payload.decode("utf-8")
    event = json.loads(raw)
    # If this raises (disk full, locked DB...), FastAPI returns 500 and Stripe redelivers.
    result = await run_in_threadpool(
        events.record_delivery, settings.database_path, event, raw, settings.max_attempts
    )
    log.info(
        "webhook %s type=%s duplicate=%s deliveries=%d",
        result.event_id,
        event.get("type"),
        result.duplicate,
        result.delivery_count,
    )
    return JSONResponse(
        {
            "received": True,
            "event_id": result.event_id,
            "duplicate": result.duplicate,
            "status": result.status,
        }
    )
