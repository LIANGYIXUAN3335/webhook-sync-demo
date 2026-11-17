"""Run every demo scenario against a running server and print a pass/fail report.

    python -m scripts.demo_all            # asks before resetting the demo data
    python -m scripts.demo_all --yes      # no prompt

It resets the demo data (events, faults, synthetic orders), then drives each scenario
over real HTTP: signed webhook deliveries, fault injection on the mock order service,
and the dashboard's replay endpoint. It waits for the background worker, so it takes
about a minute with the default retry settings (MAX_ATTEMPTS=5, RETRY_BASE_SECONDS=1).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app import synthetic
from app.config import Settings

TERMINAL = {"succeeded", "failed", "ignored"}


class ScenarioFailed(AssertionError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise ScenarioFailed(message)


def ok(resp: httpx.Response) -> httpx.Response:
    """Dashboard actions answer 303 (POST/redirect/GET); anything >= 400 is an error."""
    if resp.status_code >= 400:
        raise ScenarioFailed(f"{resp.request.method} {resp.request.url.path} -> HTTP {resp.status_code}")
    return resp


@dataclass
class Demo:
    http: httpx.Client
    secret: str
    info: dict

    # --- primitives ------------------------------------------------------------

    def send(self, event: dict, secret: str | None = None) -> httpx.Response:
        payload = synthetic.encode(event)
        return self.http.post(
            "/webhooks/stripe",
            content=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": synthetic.sign(payload, secret or self.secret),
            },
        )

    def fault(self, mode: str, count: int = 1) -> None:
        ok(self.http.post("/faults", data={"mode": mode, "count": count}))

    def event(self, event_id: str) -> dict | None:
        rows = self.http.get("/api/events", params={"q": event_id}).json()
        return next((r for r in rows if r["id"] == event_id), None)

    def order(self, order_id: str) -> dict:
        return next(o for o in self.http.get("/mock-oms/orders").json() if o["id"] == order_id)

    def wait(self, event_id: str, timeout: float = 60) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            row = self.event(event_id)
            if row and row["status"] in TERMINAL:
                return row
            time.sleep(0.25)
        raise ScenarioFailed(f"{event_id} did not reach a terminal state within {timeout:.0f}s")

    def checkout(self, order_id: str | None, **kw) -> dict:
        known = order_id is not None and order_id.startswith("ord_demo")
        amount = self.order(order_id)["amount_cents"] if known else 1000
        return synthetic.build_checkout_completed_event(order_id, amount_total=amount, **kw)

    # --- scenarios ---------------------------------------------------------------

    def happy_path(self) -> str:
        ev = self.checkout("ord_demo_1001")
        check(self.send(ev).status_code == 200, "delivery not accepted")
        row = self.wait(ev["id"])
        check(row["status"] == "succeeded" and row["attempts"] == 1, f"got {row['status']}/{row['attempts']}")
        check(self.order("ord_demo_1001")["update_count"] == 1, "order not updated exactly once")
        return "succeeded on attempt 1, order paid"

    def duplicate_delivery(self) -> str:
        ev = self.checkout("ord_demo_1002")
        bodies = [self.send(ev).json() for _ in range(3)]
        check([b["duplicate"] for b in bodies] == [False, True, True], f"dedup flags {bodies}")
        row = self.wait(ev["id"])
        check(row["delivery_count"] == 3 and row["attempts"] == 1, f"{row['delivery_count']} deliveries")
        check(self.order("ord_demo_1002")["update_count"] == 1, "order updated more than once")
        return "3 deliveries → 1 event, 1 attempt, 1 order update"

    def invalid_signature(self) -> str:
        before = len(self.http.get("/api/events").json())
        ev = self.checkout("ord_demo_1003")
        resp = self.send(ev, secret="whsec_wrong_secret")  # noqa: S106 - deliberately wrong
        check(resp.status_code == 400, f"expected 400, got {resp.status_code}")
        check(len(self.http.get("/api/events").json()) == before, "rejected event was stored")
        return "HTTP 400, nothing stored"

    def transient_then_recovery(self) -> str:
        self.fault("503", 2)
        self.fault("timeout")
        ev = self.checkout("ord_demo_1003")
        self.send(ev)
        row = self.wait(ev["id"])
        check(row["status"] == "succeeded", f"ended {row['status']}: {row['last_error']}")
        check(row["attempts"] == 4, f"expected 4 attempts, got {row['attempts']}")
        check(self.order("ord_demo_1003")["update_count"] == 1, "order not updated exactly once")
        return "503 → 503 → timeout → succeeded (4 attempts)"

    def exhausted_then_replay(self) -> str:
        n = self.info["max_attempts"]
        self.fault("503", n)
        ev = self.checkout("ord_demo_1004")
        self.send(ev)
        row = self.wait(ev["id"], timeout=self.info["retry_max_seconds"] * n + 30)
        check(row["status"] == "failed", f"expected failed, got {row['status']}")
        check(row["last_error"].startswith(f"retries exhausted after {n} attempts"), row["last_error"])
        check(self.order("ord_demo_1004")["update_count"] == 0, "failed event updated the order")
        ok(self.http.post(f"/events/{ev['id']}/replay"))
        row = self.wait(ev["id"])
        check(row["status"] == "succeeded" and row["replay_count"] == 1, f"after replay: {row['status']}")
        self.http.post(f"/events/{ev['id']}/replay")  # not failed any more → refused
        self.send(ev)  # and a late redelivery
        time.sleep(1)
        check(self.order("ord_demo_1004")["update_count"] == 1, "order updated more than once")
        return f"failed after {n} attempts → replay → succeeded; order updated once"

    def lost_response(self) -> str:
        self.fault("timeout_after_commit")
        ev = self.checkout("ord_demo_1005")
        self.send(ev)
        row = self.wait(ev["id"])
        check(row["status"] == "succeeded" and row["attempts"] == 2, f"{row['status']}/{row['attempts']}")
        check(self.order("ord_demo_1005")["update_count"] == 1, "ambiguous timeout caused a double update")
        return "commit + timeout → retry is an idempotent no-op"

    def permanent_error(self) -> str:
        ev = self.checkout("ord_does_not_exist")
        self.send(ev)
        row = self.wait(ev["id"])
        check(row["status"] == "failed" and row["attempts"] == 1, f"{row['status']}/{row['attempts']}")
        check("HTTP 404" in row["last_error"], row["last_error"])
        return "HTTP 404 → failed after 1 attempt, no retries"

    def async_payment(self) -> str:
        ev = self.checkout("ord_demo_1006", payment_status="unpaid")
        self.send(ev)
        check(self.wait(ev["id"])["status"] == "ignored", "unpaid session was not ignored")
        settled = self.checkout(
            "ord_demo_1006",
            session_id=ev["data"]["object"]["id"],
            event_type="checkout.session.async_payment_succeeded",
        )
        self.send(settled)
        check(self.wait(settled["id"])["status"] == "succeeded", "async payment did not sync")
        check(self.order("ord_demo_1006")["update_count"] == 1, "order not updated exactly once")
        return "unpaid → ignored; async_payment_succeeded → paid"


SCENARIOS: list[tuple[str, Callable[[Demo], str]]] = [
    ("Happy path", Demo.happy_path),
    ("Duplicate delivery (same event ID ×3)", Demo.duplicate_delivery),
    ("Invalid signature", Demo.invalid_signature),
    ("Transient failures, then recovery", Demo.transient_then_recovery),
    ("Retries exhausted, then manual replay", Demo.exhausted_then_replay),
    ("Response lost after commit", Demo.lost_response),
    ("Permanent error", Demo.permanent_error),
    ("Async payment (unpaid → settled)", Demo.async_payment),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--yes", action="store_true", help="reset demo data without asking")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if not settings.stripe_webhook_secret:
        print("STRIPE_WEBHOOK_SECRET is not set (copy .env.example to .env)", file=sys.stderr)
        return 2
    http = httpx.Client(base_url=args.url.rstrip("/"), timeout=10, follow_redirects=False)
    try:
        info = http.get("/api/info").json()
    except httpx.HTTPError as exc:
        print(f"Server not reachable at {args.url}: {exc}", file=sys.stderr)
        return 2
    if not (info["simulator_enabled"] and info["worker_enabled"]):
        print("Needs ENABLE_SIMULATOR=true and WORKER_ENABLED=true on the server.", file=sys.stderr)
        return 2
    if not args.yes and input("This resets all demo data on the server. Continue? [y/N] ").lower() != "y":
        return 1

    ok(http.post("/demo/reset"))
    demo = Demo(http, settings.stripe_webhook_secret, info)
    print(f"Running {len(SCENARIOS)} scenarios against {args.url} (max_attempts={info['max_attempts']})\n")
    failures = 0
    for name, scenario in SCENARIOS:
        started = time.time()
        live = sys.stdout.isatty()
        if live:
            print(f"  … {name}", end="", flush=True)
        try:
            detail, mark = scenario(demo), "PASS"
        except (ScenarioFailed, httpx.HTTPError) as exc:
            detail, mark = str(exc), "FAIL"
            failures += 1
        print(f"{chr(13) if live else ''}  {mark}  {name:<40} {time.time() - started:5.1f}s  {detail}")

    orders = http.get("/mock-oms/orders").json()
    worst = max(o["update_count"] for o in orders)
    print(f"\nMax updates applied to any single order: {worst} (must be <= 1)")
    print(f"{len(SCENARIOS) - failures}/{len(SCENARIOS)} scenarios passed. Open {args.url} to inspect them.")
    if worst > 1:
        failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
