from app import synthetic
from tests.conftest import TEST_SECRET


def post_event(client, event, secret=TEST_SECRET, timestamp=None, signature=None):
    payload = synthetic.encode(event)
    header = signature if signature is not None else synthetic.sign(payload, secret, timestamp)
    headers = {"Content-Type": "application/json"}
    if header:
        headers["Stripe-Signature"] = header
    return client.post("/webhooks/stripe", content=payload, headers=headers)
