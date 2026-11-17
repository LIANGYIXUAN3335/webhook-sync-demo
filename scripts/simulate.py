"""Offline demo client: sends signed synthetic Stripe events to a running server over HTTP.

No Stripe account or network access needed. Events are signed with STRIPE_WEBHOOK_SECRET
(from the environment / .env), the same secret the server verifies with.

Examples:
    python -m scripts.simulate checkout ord_demo_1001
    python -m scripts.simulate checkout ord_demo_1002 --deliveries 3     # duplicate delivery
    python -m scripts.simulate checkout ord_demo_1003 --bad-signature    # rejected with 400
    python -m scripts.simulate checkout ord_demo_1004 --unpaid           # stored, ignored
    python -m scripts.simulate fault 503 --count 2                       # make the OMS flaky
    python -m scripts.simulate events                                    # print event states
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

from app import db, synthetic
from app.config import Settings


def cmd_checkout(args, settings: Settings) -> int:
    amounts = {o[0]: (o[3], o[4]) for o in db.SYNTHETIC_ORDERS}
    amount, currency = amounts.get(args.order_id, (1000, "usd"))
    event = synthetic.build_checkout_completed_event(
        args.order_id, amount_total=amount, currency=currency,
        payment_status="unpaid" if args.unpaid else "paid",
        event_type=args.type,
    )
    payload = synthetic.encode(event)
    secret = "whsec_wrong_secret" if args.bad_signature else settings.stripe_webhook_secret
    if not secret:
        print("STRIPE_WEBHOOK_SECRET is not set (copy .env.example to .env)", file=sys.stderr)
        return 2
    for i in range(args.deliveries):
        resp = httpx.post(
            f"{args.url}/webhooks/stripe", content=payload,
            headers={"Content-Type": "application/json",
                     "Stripe-Signature": synthetic.sign(payload, secret)},
        )
        print(f"delivery {i + 1}: HTTP {resp.status_code} {resp.text}")
    return 0


def cmd_fault(args, settings: Settings) -> int:
    resp = httpx.post(f"{args.url}/faults", data={"mode": args.mode, "count": args.count})
    print(f"queued {args.count} x {args.mode}: HTTP {resp.status_code}")
    return 0 if resp.status_code < 400 else 1


def cmd_events(args, settings: Settings) -> int:
    rows = httpx.get(f"{args.url}/api/events").json()
    for r in rows:
        print(json.dumps({k: r[k] for k in ("id", "order_id", "status", "attempts",
                                              "delivery_count", "last_error")}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="server base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("checkout", help="send checkout.session.completed")
    p.add_argument("order_id")
    p.add_argument("--deliveries", type=int, default=1, help="send the same event N times")
    p.add_argument("--unpaid", action="store_true", help="payment_status=unpaid")
    p.add_argument("--bad-signature", action="store_true", help="sign with the wrong secret")
    p.add_argument("--type", default="checkout.session.completed",
                   choices=["checkout.session.completed", "checkout.session.async_payment_succeeded",
                            "checkout.session.expired"])
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("fault", help="queue faults on the mock order service")
    p.add_argument("mode", choices=["503", "500", "429", "timeout", "timeout_after_commit", "400"])
    p.add_argument("--count", type=int, default=1)
    p.set_defaults(func=cmd_fault)

    p = sub.add_parser("events", help="list events and their sync state")
    p.set_defaults(func=cmd_events)

    args = parser.parse_args(argv)
    args.url = args.url.rstrip("/")
    return args.func(args, Settings.from_env())


if __name__ == "__main__":
    raise SystemExit(main())
