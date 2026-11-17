"""Create a real Checkout Session in a Stripe *sandbox* for one of the synthetic orders.

Pay it in the browser with a test card (4242 4242 4242 4242, any future date, any CVC);
Stripe then sends checkout.session.completed, which `stripe listen` forwards to this app.

    python -m scripts.create_checkout_session ord_demo_1002

Reads STRIPE_API_KEY from the environment / .env and refuses anything but test-mode keys.
"""

from __future__ import annotations

import argparse
import sys

import stripe

from app import db
from app.config import Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("order_id", choices=[o[0] for o in db.SYNTHETIC_ORDERS])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000",
                        help="where the browser returns after checkout")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    key = settings.stripe_api_key
    if not key:
        print("STRIPE_API_KEY is not set (see .env.example)", file=sys.stderr)
        return 2
    if not key.startswith(("sk_test_", "rk_test_")):
        print("Refusing to run: STRIPE_API_KEY must be a sandbox/test key (sk_test_/rk_test_).",
              file=sys.stderr)
        return 2

    order = next(o for o in db.SYNTHETIC_ORDERS if o[0] == args.order_id)
    order_id, _name, _email, amount, currency = order
    client = stripe.StripeClient(key)
    session = client.v1.checkout.sessions.create(params={
        "mode": "payment",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": currency,
                "unit_amount": amount,
                "product_data": {"name": f"Synthetic demo order {order_id}"},
            },
        }],
        # Both carry the order reference; the webhook handler reads metadata first.
        "client_reference_id": order_id,
        "metadata": {"order_id": order_id},
        "success_url": f"{args.base_url}/?msg=Checkout+completed+for+{order_id}&level=ok",
        "cancel_url": f"{args.base_url}/?msg=Checkout+canceled&level=warn",
    })
    print(f"Checkout Session {session.id} for {order_id} ({amount / 100:.2f} {currency.upper()})")
    print(f"Open and pay with test card 4242 4242 4242 4242:\n{session.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
