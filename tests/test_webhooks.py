import json
import time

import pytest

from app import db, events, synthetic
from tests.helpers import post_event


def _event_count(settings):
    with db.session(settings.database_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_valid_signature_persists_event_before_2xx(client, settings):
    event = synthetic.build_checkout_completed_event("ord_demo_1001")
    resp = post_event(client, event)
    assert resp.status_code == 200
    assert resp.json()["duplicate"] is False

    row = events.get_event(settings.database_path, event["id"])
    assert row is not None
    assert row["status"] == "pending"
    assert row["order_id"] == "ord_demo_1001"
    assert row["checkout_session_id"] == event["data"]["object"]["id"]
    assert row["payload"] == synthetic.encode(event)  # raw body stored verbatim


@pytest.mark.parametrize(
    "case",
    ["wrong_secret", "missing_header", "garbage_header", "stale_timestamp", "tampered_body"],
)
def test_invalid_signature_is_rejected_and_not_stored(client, settings, case):
    event = synthetic.build_checkout_completed_event("ord_demo_1001")
    payload = synthetic.encode(event)
    headers = {"Content-Type": "application/json"}
    if case == "wrong_secret":
        headers["Stripe-Signature"] = synthetic.sign(payload, "whsec_someone_else")
    elif case == "garbage_header":
        headers["Stripe-Signature"] = "t=123,v1=deadbeef"
    elif case == "stale_timestamp":
        # Correctly signed, but outside Stripe's default 5-minute tolerance (replay attack).
        headers["Stripe-Signature"] = synthetic.sign(
            payload, settings.stripe_webhook_secret, int(time.time()) - 3600
        )
    elif case == "tampered_body":
        headers["Stripe-Signature"] = synthetic.sign(payload, settings.stripe_webhook_secret)
        payload = payload.replace('"amount_total":2500', '"amount_total":1')
    resp = client.post("/webhooks/stripe", content=payload, headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid signature"}
    assert _event_count(settings) == 0


def test_signature_is_checked_against_raw_bytes_not_reserialized_json(client, settings):
    """Whitespace changes the HMAC; a receiver that re-serializes JSON would get this wrong."""
    event = synthetic.build_checkout_completed_event("ord_demo_1001")
    pretty = json.dumps(event, indent=2)
    headers = {"Stripe-Signature": synthetic.sign(pretty, settings.stripe_webhook_secret)}
    assert client.post("/webhooks/stripe", content=pretty, headers=headers).status_code == 200


def test_duplicate_delivery_is_deduplicated_by_event_id(client, settings):
    event = synthetic.build_checkout_completed_event("ord_demo_1002")
    first = post_event(client, event)
    second = post_event(client, event)  # Stripe redelivers the same event ID
    third = post_event(client, event)

    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True and third.json()["duplicate"] is True
    assert _event_count(settings) == 1
    row = events.get_event(settings.database_path, event["id"])
    assert row["delivery_count"] == 3
    assert row["status"] == "pending"  # still exactly one unit of work


def test_unhandled_event_type_is_stored_as_ignored(client, settings):
    event = synthetic.build_checkout_completed_event("ord_demo_1001", event_type="checkout.session.expired")
    assert post_event(client, event).status_code == 200
    assert events.get_event(settings.database_path, event["id"])["status"] == "ignored"


def test_missing_webhook_secret_fails_closed(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.stripe_webhook_secret = ""
    with TestClient(create_app(settings)) as c:
        event = synthetic.build_checkout_completed_event("ord_demo_1001")
        assert post_event(c, event).status_code == 500
    assert _event_count(settings) == 0
