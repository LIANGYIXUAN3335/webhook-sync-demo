"""Async payment flow, bulk replay, redirect safety, and concurrent workers."""

import json
import threading
import time

import httpx

from app import events, oms, synthetic
from app.worker import OrderApiClient, Worker
from tests.helpers import post_event


def test_async_payment_unpaid_then_succeeded_updates_order_once(client, settings, worker):
    completed = synthetic.build_checkout_completed_event("ord_demo_1001", payment_status="unpaid")
    session_id = completed["data"]["object"]["id"]
    post_event(client, completed)
    worker.run_until_idle()
    row = events.get_event(settings.database_path, completed["id"])
    assert row["status"] == "ignored"
    assert "async_payment_succeeded" in row["last_error"]
    assert oms.get_order(settings.database_path, "ord_demo_1001")["status"] == "pending"

    settled = synthetic.build_checkout_completed_event(
        "ord_demo_1001", session_id=session_id,
        event_type="checkout.session.async_payment_succeeded",
    )
    post_event(client, settled)
    post_event(client, settled)  # redelivered
    worker.run_until_idle()
    assert events.get_event(settings.database_path, settled["id"])["status"] == "succeeded"
    order = oms.get_order(settings.database_path, "ord_demo_1001")
    assert order["status"] == "paid" and order["paid_session_id"] == session_id
    assert order["update_count"] == 1


def test_simulator_settle_payment_action(client, settings, worker):
    client.post("/simulate/checkout", data={"order_id": "ord_demo_1002", "payment_status": "unpaid"})
    worker.run_until_idle()
    [unpaid] = events.list_events(settings.database_path)
    assert unpaid["status"] == "ignored"
    assert "Settle payment" in client.get("/").text

    client.post(f"/simulate/async-succeeded/{unpaid['id']}")
    worker.run_until_idle()
    settled = [e for e in events.list_events(settings.database_path) if e["id"] != unpaid["id"]]
    assert [e["type"] for e in settled] == ["checkout.session.async_payment_succeeded"]
    assert settled[0]["checkout_session_id"] == unpaid["checkout_session_id"]
    assert settled[0]["status"] == "succeeded"
    assert oms.get_order(settings.database_path, "ord_demo_1002")["update_count"] == 1


def test_replay_all_failed(client, settings, worker, clock):
    oms.push_faults(settings.database_path, "400", 2)
    first = synthetic.build_checkout_completed_event("ord_demo_1001")
    second = synthetic.build_checkout_completed_event("ord_demo_1002")
    post_event(client, first)
    post_event(client, second)
    worker.run_until_idle()
    assert events.status_counts(settings.database_path) == {"failed": 2}

    resp = client.post("/events/replay-failed", follow_redirects=False)
    assert "2+failed+event" in resp.headers["location"]
    worker.run_until_idle()
    assert events.status_counts(settings.database_path) == {"succeeded": 2}
    assert client.post("/events/replay-failed", follow_redirects=False).headers["location"].endswith(
        "msg=No+failed+events+to+replay&level=info")


def test_flash_redirect_never_leaves_the_site(client):
    for referer, expected_prefix in [
        ("https://evil.example/phish?x=1", "/phish?x=1&msg="),
        ("http://testserver//evil.example/path", "/?msg="),
        ("http://testserver/?status=failed&msg=old&level=warn", "/?status=failed&msg=No+failed"),
    ]:
        resp = client.post("/events/replay-failed", headers={"Referer": referer}, follow_redirects=False)
        assert resp.headers["location"].startswith(expected_prefix), (referer, resp.headers["location"])


def test_concurrent_workers_update_each_order_once(client, settings):
    """Four workers race over one queue that contains duplicates (same event ID) and
    distinct events for the same Checkout Session. Every order must change exactly once
    and every event must be attempted exactly once."""
    order_ids = ["ord_demo_1001", "ord_demo_1002", "ord_demo_1003", "ord_demo_1004",
                 "ord_demo_1005", "ord_demo_1006"]
    sent = []
    for oid in order_ids:
        ev = synthetic.build_checkout_completed_event(oid)
        twin = synthetic.build_checkout_completed_event(oid, session_id=ev["data"]["object"]["id"])
        for e in (ev, ev, twin):
            assert post_event(client, e).status_code == 200
        sent += [ev["id"], twin["id"]]

    def handler(request: httpx.Request) -> httpx.Response:
        order_id = request.url.path.split("/")[2]
        status, body, replayed = oms.apply_payment(
            settings.database_path, order_id, request.headers.get("idempotency-key"),
            json.loads(request.content),
        )
        time.sleep(0.002)  # widen the race window
        return httpx.Response(status, json=body,
                              headers={"Idempotent-Replayed": "true"} if replayed else {})

    workers = [
        Worker(settings, OrderApiClient("http://oms.test", 2,
                                        httpx.Client(transport=httpx.MockTransport(handler))))
        for _ in range(4)
    ]
    start = threading.Barrier(len(workers))

    def run(w):
        start.wait()
        w.run_until_idle()

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    rows = [events.get_event(settings.database_path, eid) for eid in sent]
    assert all(r["status"] == "succeeded" for r in rows)
    assert all(r["attempts"] == 1 for r in rows)  # no event was processed twice
    for oid in order_ids:
        assert oms.get_order(settings.database_path, oid)["update_count"] == 1
