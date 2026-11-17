from app import events, oms, synthetic
from tests.helpers import post_event


def deliver(client, order_id="ord_demo_1001"):
    event = synthetic.build_checkout_completed_event(order_id)
    assert post_event(client, event).status_code == 200
    return event


def ev(settings, event_id):
    return events.get_event(settings.database_path, event_id)


def update_count(settings, order_id="ord_demo_1001"):
    return oms.get_order(settings.database_path, order_id)["update_count"]


def exhaust(worker, clock, settings):
    for _ in range(settings.max_attempts):
        worker.run_once()
        clock.advance(settings.retry_max_seconds)


def test_manual_replay_recovers_failed_event(client, settings, worker, clock):
    oms.push_faults(settings.database_path, "503", settings.max_attempts)
    event = deliver(client)
    exhaust(worker, clock, settings)
    assert ev(settings, event["id"])["status"] == "failed"
    assert update_count(settings) == 0

    resp = client.post(f"/events/{event['id']}/replay", follow_redirects=False)
    assert resp.status_code == 303 and "Replay+queued" in resp.headers["location"]
    row = ev(settings, event["id"])
    assert row["status"] == "pending"
    assert row["cycle_attempts"] == 0 and row["replay_count"] == 1
    assert row["attempts"] == settings.max_attempts  # history is kept

    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded"
    assert row["attempts"] == settings.max_attempts + 1
    assert update_count(settings) == 1


def test_replay_after_ambiguous_commit_does_not_double_update(client, settings, worker, transport, clock):
    """Every attempt timed out, but the first one had actually been applied downstream.
    Replaying the failed event must converge to 'succeeded' without a second update."""
    transport.script = ["timeout_after_commit", "timeout", "timeout"]
    event = deliver(client)
    exhaust(worker, clock, settings)
    assert ev(settings, event["id"])["status"] == "failed"
    assert update_count(settings) == 1

    client.post(f"/events/{event['id']}/replay")
    worker.run_once()
    assert ev(settings, event["id"])["status"] == "succeeded"
    assert update_count(settings) == 1
    assert "already applied" in events.list_attempts(settings.database_path, event["id"])[-1]["error"]


def test_replay_is_refused_unless_failed(client, settings, worker):
    event = deliver(client)
    worker.run_once()
    resp = client.post(f"/events/{event['id']}/replay", follow_redirects=False)
    assert "not+in+%27failed%27" in resp.headers["location"]
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded" and row["replay_count"] == 0
    assert worker.run_until_idle() == 0
    assert update_count(settings) == 1


def test_replays_and_redeliveries_together_update_order_once(client, settings, worker, clock):
    oms.push_faults(settings.database_path, "503", settings.max_attempts)
    event = deliver(client)
    exhaust(worker, clock, settings)
    client.post(f"/events/{event['id']}/replay")
    client.post(f"/events/{event['id']}/replay")  # double click: second is a no-op
    post_event(client, event)  # plus a Stripe redelivery
    client.post(f"/simulate/redeliver/{event['id']}")
    worker.run_until_idle(now=clock.now + 3600)
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded"
    assert row["replay_count"] == 1
    assert row["delivery_count"] == 3
    assert update_count(settings) == 1


def test_dashboard_shows_status_attempts_and_last_error(client, settings, worker, clock):
    oms.push_faults(settings.database_path, "503")
    event = deliver(client, "ord_demo_1003")
    worker.run_once()

    page = client.get("/")
    assert page.status_code == 200
    html = page.text
    assert event["id"][:22] in html
    assert "badge-retrying" in html
    assert "HTTP 503: injected fault 503" in html
    assert "1/3" in html  # attempts / budget

    assert client.get("/?status=retrying").status_code == 200
    assert event["id"][:22] in client.get("/partials/live").text
    detail = client.get(f"/events/{event['id']}")
    assert detail.status_code == 200
    assert "Attempt log" in detail.text and "ord_demo_1003" in detail.text
    assert client.get("/events/evt_missing").status_code == 404


def test_simulator_posts_through_signature_verification(client, settings):
    client.post("/simulate/checkout", data={"order_id": "ord_demo_1002", "signature": "invalid"})
    assert events.list_events(settings.database_path) == []

    resp = client.post("/simulate/checkout", data={"order_id": "ord_demo_1002"}, follow_redirects=False)
    assert resp.status_code == 303
    [row] = events.list_events(settings.database_path)
    assert row["order_id"] == "ord_demo_1002" and row["status"] == "pending"

    client.post(f"/simulate/redeliver/{row['id']}")
    assert ev(settings, row["id"])["delivery_count"] == 2


def test_fault_controls_and_reset(client, settings):
    client.post("/faults", data={"mode": "429", "count": 2})
    assert oms.list_faults(settings.database_path) == ["429", "429"]
    client.post("/faults/clear")
    assert oms.list_faults(settings.database_path) == []
    deliver(client)
    client.post("/demo/reset")
    assert events.list_events(settings.database_path) == []


def test_simulator_can_be_disabled(settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings.enable_simulator = False
    with TestClient(create_app(settings)) as c:
        assert c.post("/simulate/checkout", data={"order_id": "ord_demo_1001"}).status_code == 404
        assert c.post("/faults", data={"mode": "503"}).status_code == 404
        assert "Offline simulator" not in c.get("/").text


def test_search_filters_by_order_event_or_session_id(client, settings):
    a = deliver(client, "ord_demo_1001")
    b = deliver(client, "ord_demo_1002")
    html = client.get("/?q=ord_demo_1002").text
    assert b["id"][:22] in html and a["id"][:22] not in html
    assert a["id"][:22] in client.get(f"/partials/live?q={a['data']['object']['id']}").text
    assert "No events match" in client.get("/?q=nothing-here&status=failed").text


def test_event_detail_shows_idempotency_key_and_backoff_timeline(client, settings, worker, clock):
    oms.push_faults(settings.database_path, "503")
    event = deliver(client)
    worker.run_once()
    clock.advance(1)
    worker.run_once()
    html = client.get(f"/events/{event['id']}").text
    assert f"checkout_session:{event['data']['object']['id']}" in html
    assert "+1.0 s" in html  # waited one backoff interval before attempt 2
    assert "window.AUTO_RELOAD = false" in html  # terminal: no auto-reload
