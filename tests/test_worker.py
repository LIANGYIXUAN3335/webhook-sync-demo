import pytest

from app import events, oms, synthetic
from tests.helpers import post_event


def deliver(client, order_id="ord_demo_1001", **kw):
    event = synthetic.build_checkout_completed_event(order_id, **kw)
    assert post_event(client, event).status_code == 200
    return event


def ev(settings, event_id):
    return events.get_event(settings.database_path, event_id)


def order(settings, order_id="ord_demo_1001"):
    return oms.get_order(settings.database_path, order_id)


def attempts(settings, event_id):
    return [(a["outcome"], a["http_status"]) for a in events.list_attempts(settings.database_path, event_id)]


def test_happy_path_marks_order_paid_once(client, settings, worker):
    event = deliver(client)
    assert worker.run_until_idle() == 1

    row = ev(settings, event["id"])
    assert row["status"] == "succeeded"
    assert row["attempts"] == 1
    assert row["last_error"] is None
    o = order(settings)
    assert o["status"] == "paid"
    assert o["paid_session_id"] == event["data"]["object"]["id"]
    assert o["update_count"] == 1


def test_transient_failures_then_recovery(client, settings, worker, transport, clock):
    """429 -> 503 -> timeout -> success, with backoff between attempts."""
    settings.max_attempts = 5
    oms.push_faults(settings.database_path, "429")
    oms.push_faults(settings.database_path, "503")
    transport.script = ["forward", "forward", "timeout"]
    event = deliver(client)

    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "retrying"
    assert "HTTP 429" in row["last_error"]
    # backoff for attempt 1 is 1s, but Retry-After: 2 from the 429 wins
    assert row["next_attempt_at"] == pytest.approx(clock.now + 2)

    clock.advance(1)
    assert worker.run_once() is None  # not due yet: backoff is respected
    clock.advance(1)
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "retrying" and "HTTP 503" in row["last_error"]
    assert row["next_attempt_at"] == pytest.approx(clock.now + 2)

    clock.advance(2)
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "retrying" and "timeout" in row["last_error"]
    assert row["next_attempt_at"] == pytest.approx(clock.now + 4)
    assert order(settings)["status"] == "pending"

    clock.advance(4)
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded"
    assert row["attempts"] == 4
    assert order(settings)["status"] == "paid"
    assert order(settings)["update_count"] == 1
    assert attempts(settings, event["id"]) == [
        ("retry", 429),
        ("retry", 503),
        ("retry", None),
        ("succeeded", 200),
    ]


def test_ambiguous_timeout_after_commit_does_not_double_update(client, settings, worker, transport, clock):
    """The order service applied the update but the response was lost. The retry must be
    recognized as the same operation (Idempotency-Key) instead of paying again."""
    transport.script = ["timeout_after_commit"]
    event = deliver(client)

    worker.run_once()
    assert ev(settings, event["id"])["status"] == "retrying"
    assert order(settings)["update_count"] == 1  # already applied downstream

    clock.advance(10)
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded"
    assert order(settings)["update_count"] == 1
    last = events.list_attempts(settings.database_path, event["id"])[-1]
    assert "already applied" in last["error"]


def test_retries_exhausted_moves_to_failed(client, settings, worker, transport, clock):
    oms.push_faults(settings.database_path, "503", 2)
    transport.script = ["forward", "forward", "network"]  # 3rd attempt: connection refused
    event = deliver(client)

    for _ in range(settings.max_attempts):
        assert worker.run_once() == event["id"]
        clock.advance(settings.retry_max_seconds)

    row = ev(settings, event["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == settings.max_attempts == 3
    assert row["last_error"].startswith("retries exhausted after 3 attempts")
    assert "network error" in row["last_error"]
    assert row["next_attempt_at"] is None
    assert worker.run_once() is None  # nothing left to do: failed is terminal
    assert order(settings)["status"] == "pending"


def test_backoff_is_exponential_and_capped(settings, worker):
    settings.retry_base_seconds, settings.retry_max_seconds = 1, 10
    assert [worker.backoff_delay(n) for n in range(1, 7)] == [1, 2, 4, 8, 10, 10]
    assert worker.backoff_delay(1, retry_after=7) == 7
    assert worker.backoff_delay(1, retry_after=999) == 10  # Retry-After is capped too
    settings.retry_jitter = True
    worker.rng = lambda: 0.0
    assert worker.backoff_delay(3) == 2  # equal jitter: between 50% and 100%


@pytest.mark.parametrize("fault,expected", [("400", "HTTP 400"), (None, "HTTP 404")])
def test_permanent_errors_fail_without_retry(client, settings, worker, fault, expected):
    if fault:
        oms.push_faults(settings.database_path, fault)
        event = deliver(client)
    else:
        event = deliver(client, order_id="ord_does_not_exist")
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "failed"
    assert row["attempts"] == 1
    assert expected in row["last_error"]


def test_event_without_order_reference_fails_permanently(client, settings, worker, transport):
    event = deliver(client, order_id=None)
    worker.run_once()
    row = ev(settings, event["id"])
    assert row["status"] == "failed"
    assert "no order reference" in row["last_error"]
    assert transport.calls == 0  # never reached the order service


def test_unpaid_session_is_ignored(client, settings, worker, transport):
    event = deliver(client, payment_status="unpaid")
    worker.run_once()
    assert ev(settings, event["id"])["status"] == "ignored"
    assert order(settings)["status"] == "pending"
    assert transport.calls == 0


def test_redelivery_after_success_does_not_resync(client, settings, worker, transport):
    event = synthetic.build_checkout_completed_event("ord_demo_1001")
    post_event(client, event)
    worker.run_until_idle()
    post_event(client, event)  # Stripe redelivers (e.g. `stripe events resend`)
    post_event(client, event)
    assert worker.run_until_idle() == 0
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded" and row["delivery_count"] == 3 and row["attempts"] == 1
    assert transport.calls == 1
    assert order(settings)["update_count"] == 1


def test_distinct_events_for_same_session_update_order_once(client, settings, worker):
    first = deliver(client)
    session_id = first["data"]["object"]["id"]
    second = deliver(client, session_id=session_id)  # different event ID, same payment
    worker.run_until_idle()
    assert ev(settings, first["id"])["status"] == "succeeded"
    assert ev(settings, second["id"])["status"] == "succeeded"
    assert order(settings)["update_count"] == 1


def test_second_session_for_paid_order_is_flagged_not_applied(client, settings, worker):
    deliver(client)
    other = deliver(client)  # a different checkout session for an already-paid order
    worker.run_until_idle()
    row = ev(settings, other["id"])
    assert row["status"] == "failed"
    assert "HTTP 409" in row["last_error"]
    assert order(settings)["update_count"] == 1


def test_expired_processing_lease_is_reclaimed(client, settings, worker, clock):
    event = deliver(client)
    claimed = worker.claim_next(clock())  # simulate a worker that crashed mid-attempt
    assert claimed is not None
    assert worker.run_once() is None  # lease still held
    clock.advance(settings.processing_lease_seconds + 1)
    assert worker.run_once() == event["id"]
    row = ev(settings, event["id"])
    assert row["status"] == "succeeded" and row["attempts"] == 2
    assert order(settings)["update_count"] == 1


def test_stale_worker_cannot_overwrite_newer_result(client, settings, worker, clock):
    from app.worker import Outcome

    event = deliver(client)
    stale_row, stale_attempt = worker.claim_next(clock())
    clock.advance(settings.processing_lease_seconds + 1)
    worker.run_once()  # another worker reclaims and succeeds
    worker._finish(stale_row, stale_attempt, Outcome("failed", "late result"), clock())
    assert ev(settings, event["id"])["status"] == "succeeded"
