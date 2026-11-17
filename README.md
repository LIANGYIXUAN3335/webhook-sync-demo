# Reliable Stripe Webhook Sync with Retries and an Admin Dashboard

A small but complete reference implementation of the "receive Stripe webhooks safely, then
sync to a downstream system" pattern, in Python/FastAPI + SQLite with server-rendered
templates.

- Listens for `checkout.session.completed` from a Stripe **sandbox** and verifies the
  signature over the **raw request body** with the official `stripe.Webhook.construct_event`.
- **Persists the event before returning 2xx**, deduplicated by Stripe event ID.
- A **background worker** syncs the order status to an order service over HTTP. Timeouts,
  network errors, 429 and 5xx are retried with **capped exponential backoff**. Other 4xx
  and malformed events go straight to **failed**, and so does an event that uses up its
  retry budget.
- An **admin dashboard** shows each event's status, attempt count, deliveries, next retry
  and last error, with a per-event attempt log and **manual replay** of failed events.
- **Neither replays nor duplicate deliveries can update the same order twice.**
- Two reproducible demo paths: the **Stripe CLI** against a sandbox, and a **fully offline
  simulator**. All order data is synthetic (`*@example.test`).

---

## Quick start

Requires Python 3.10+ (developed on 3.12).

```bash
python3 -m venv .venv            # or: uv venv --python 3.12 .venv
.venv/bin/pip install -r requirements.txt   # or: uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env             # the defaults work for the offline demo
.venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
```

Open **http://127.0.0.1:8000**. The SQLite file is created at `data/webhook_sync.db`, and
six synthetic orders (`ord_demo_1001` … `ord_demo_1006`) are seeded.

Run the tests:

```bash
.venv/bin/python -m pytest -v
```

---

## Data flow

```
 Stripe sandbox ──(stripe listen)──┐
                                   ├─► POST /webhooks/stripe
 Offline simulator (signed) ───────┘        │ 1. read raw body + Stripe-Signature
                                            │ 2. stripe.Webhook.construct_event(...)   ✗ → 400, nothing stored
                                            │ 3. INSERT event (PK = event id)          dup → delivery_count += 1
                                            │ 4. COMMIT, then 200                      DB error → 5xx, Stripe redelivers
                                            ▼
                                     SQLite  events / event_attempts
                                            ▲ │
                       claim due event      │ │  (BEGIN IMMEDIATE, lease + attempt fencing)
                                            │ ▼
                                     Worker thread ──HTTP──► Order service  POST /orders/{id}/payments
                                                              Idempotency-Key: checkout_session:<cs_id>
                                                              (mock at /mock-oms, with fault injection)
                                            │
                                            ▼
                                     Admin dashboard  /  (status, attempts, last error, replay)
```

### Event state machine

```
pending ──claim──► processing ──2xx──────────────────────────────► succeeded
                       │  ├──timeout / network / 429 / 5xx──► retrying ──(backoff)──► processing
                       │  ├──other 4xx, no order reference ──────────────────────────► failed
                       │  └──retry budget exhausted ─────────────────────────────────► failed
                       └──payment_status ≠ paid ─────────────────────────────────────► ignored
failed ──(manual replay: fresh budget)──► pending
```

Event types other than `checkout.session.completed` are verified and stored as `ignored`.

### Retry policy

| Result of calling the order service | Classification | Action |
|---|---|---|
| 2xx | success | `succeeded` |
| timeout (`ORDER_API_TIMEOUT_SECONDS`), connection error | transient | retry |
| 429 | transient | retry; `Retry-After` is honored as a lower bound |
| 5xx | transient | retry |
| other 4xx (400, 404, 409, 422) | permanent | `failed` immediately |
| event has no `metadata.order_id` / `client_reference_id` | permanent | `failed`, order service never called |

Delay before retry *n* = `min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS × 2^(n-1))`, with
"equal jitter" (50–100 % of that value). After `MAX_ATTEMPTS` attempts in one cycle, the
event becomes `failed` with `retries exhausted after N attempts; last error: …`.

### Why an order can't be updated twice

Four independent layers:

1. **Event ID dedup.** `events.id` is the primary key. A redelivery only increments
   `delivery_count`. It never re-queues the event or touches its sync state.
2. **One worker per event.** Claiming is an atomic update under `BEGIN IMMEDIATE`. A claim
   is a lease that another worker can reclaim if the first one crashes. The attempt counter
   acts as a fencing token, so a worker whose lease expired can't overwrite a newer result.
3. **Idempotency key per payment.** The worker sends
   `Idempotency-Key: checkout_session:<cs_id>`. The key is the Checkout Session, not the
   event, so even two *different* events about the same payment collapse into one update.
   A repeated key returns the stored response with `Idempotent-Replayed: true`.
4. **Conditional state transition.** The order service only moves `pending → paid`. If a
   *different* Checkout Session tries to pay an already-paid order, it gets `409` (a
   permanent failure that needs a human), not a second update.

This covers the hard case: the order service commits the update, but the response is lost
to a timeout. The retry, or a later manual replay, is recognized as the same operation and
becomes a no-op. The dashboard's **Updates** column on each order shows this: it must
never exceed 1.

---

## Demo path A: fully offline (no Stripe account, no network)

The offline simulator builds a synthetic `checkout.session.completed` payload and signs it
with `STRIPE_WEBHOOK_SECRET`, using Stripe's documented `t=…,v1=HMAC-SHA256` scheme. It
then sends it to the real `/webhooks/stripe` endpoint, so it goes through the same official
signature verification, persistence and dedup as a real delivery.

Start the server (Quick start), open http://127.0.0.1:8000, and work through these
scenarios. The dashboard refreshes itself every 1.5 s. Use **Reset demo data** to start
over.

| # | Scenario | In the dashboard | Expected |
|---|---|---|---|
| 1 | Happy path | Simulator: `ord_demo_1001`, *Send* | event `succeeded`, 1/5 attempts; order `paid`, Updates = 1 |
| 2 | Duplicate delivery | Click **Redeliver** on that event (twice) | flash says "deduplicated"; Deliveries = 3; still 1 attempt; Updates = 1 |
| 3 | Invalid signature | Simulator: Signature = *invalid* | flash "Webhook endpoint answered 400"; nothing stored |
| 4 | Transient failures → recovery | Fault injection: `503` × 2, then `timeout` × 1; send `ord_demo_1002` | `retrying` with a countdown and last error, then `succeeded` after 4 attempts (≈ 10 s) |
| 5 | Retries exhausted | Fault: `503` × 5; send `ord_demo_1003` | after ≈ 15 s: `failed`, "retries exhausted after 5 attempts" |
| 6 | Manual replay | Click **Replay** on the failed event | `pending` → `succeeded`, "replayed ×1", total attempts (6); Updates = 1 |
| 7 | Lost response | Fault: `timeout_after_commit`; send `ord_demo_1004` | first attempt times out but the order is already paid; retry ends as an idempotent no-op; Updates = 1 |
| 8 | Permanent error | Send `ord_does_not_exist` (404), or queue fault `400` | `failed` after 1 attempt, no retries |
| 9 | Unpaid session | payment_status = *unpaid* | `ignored`; order untouched |

Click any event ID to see its attempt log (timestamps show the backoff), the verified raw
payload, and the linked order.

The same flows also work from a terminal, sending real HTTP requests to the running server:

```bash
.venv/bin/python -m scripts.simulate checkout ord_demo_1001
.venv/bin/python -m scripts.simulate checkout ord_demo_1002 --deliveries 3   # duplicate delivery
.venv/bin/python -m scripts.simulate checkout ord_demo_1003 --bad-signature  # → HTTP 400
.venv/bin/python -m scripts.simulate fault 503 --count 5                     # then send an event
.venv/bin/python -m scripts.simulate events                                  # print sync state
```

## Demo path B: Stripe CLI + sandbox

Requires the [Stripe CLI](https://docs.stripe.com/stripe-cli) and a Stripe sandbox
(test mode). Never use live keys: the helper script refuses anything but `sk_test_` /
`rk_test_`.

1. Log in and get the CLI's webhook signing secret:
   ```bash
   stripe login
   stripe listen --print-secret
   ```
   Put that `whsec_…` value in `.env` as `STRIPE_WEBHOOK_SECRET`. To create real Checkout
   Sessions (step 4a), also set `STRIPE_API_KEY=sk_test_…`.
2. Start the app (Quick start).
3. In a second terminal, forward sandbox events to the app:
   ```bash
   stripe listen --events checkout.session.completed --forward-to 127.0.0.1:8000/webhooks/stripe
   ```
4. Produce a `checkout.session.completed` event:
   - **(a) Real Checkout for a synthetic order (recommended):**
     ```bash
     .venv/bin/python -m scripts.create_checkout_session ord_demo_1002
     ```
     Open the printed URL and pay with test card `4242 4242 4242 4242` (any future expiry
     date, any CVC). The session carries `metadata.order_id` and `client_reference_id`, so
     the worker marks `ord_demo_1002` as paid, and the success URL takes you back to the
     dashboard.
   - **(b) `stripe trigger`:**
     ```bash
     stripe trigger checkout.session.completed --add checkout_session:metadata.order_id=ord_demo_1003
     ```
     Without the `--add`, the fixture session has no order reference, and the event lands
     in `failed` as a permanent error. That's also a useful demo. If your CLI version
     rejects this `--add` syntax, see `stripe trigger --help`.
5. To try retries and replay against real events, queue faults in the dashboard's **Fault
   injection** panel before step 4. To test duplicates, use **Redeliver** on the event:
   it re-sends the stored raw body with a fresh signature. Alternatively, resend the event
   from the Stripe Dashboard or CLI.

---

## Tests

```bash
.venv/bin/python -m pytest -v
```

The tests drive the worker directly, with a fake clock and an HTTP transport that routes
to the in-app mock order service and can inject timeouts and connection errors. So the
retry and backoff behavior is deterministic and needs no sleeps or network.

| Requirement | Tests |
|---|---|
| Invalid signature | `test_webhooks.py::test_invalid_signature_is_rejected_and_not_stored` (wrong secret, missing/garbage header, stale timestamp, tampered body), `test_signature_is_checked_against_raw_bytes_not_reserialized_json`, `test_missing_webhook_secret_fails_closed` |
| Duplicate delivery | `test_webhooks.py::test_duplicate_delivery_is_deduplicated_by_event_id`, `test_worker.py::test_redelivery_after_success_does_not_resync`, `test_worker.py::test_distinct_events_for_same_session_update_order_once` |
| Transient failure then recovery | `test_worker.py::test_transient_failures_then_recovery` (429 → 503 → timeout → success, with exact backoff timings), `test_ambiguous_timeout_after_commit_does_not_double_update` |
| Retries exhausted | `test_worker.py::test_retries_exhausted_moves_to_failed`, `test_backoff_is_exponential_and_capped` |
| Permanent errors | `test_worker.py::test_permanent_errors_fail_without_retry`, `test_event_without_order_reference_fails_permanently`, `test_second_session_for_paid_order_is_flagged_not_applied` |
| Manual replay | `test_replay_and_dashboard.py::test_manual_replay_recovers_failed_event`, `test_replay_after_ambiguous_commit_does_not_double_update`, `test_replay_is_refused_unless_failed`, `test_replays_and_redeliveries_together_update_order_once` |
| Worker safety | `test_expired_processing_lease_is_reclaimed`, `test_stale_worker_cannot_overwrite_newer_result` |
| Dashboard / simulator | `test_dashboard_shows_status_attempts_and_last_error`, `test_simulator_posts_through_signature_verification`, `test_fault_controls_and_reset`, `test_simulator_can_be_disabled` |

---

## Configuration

All configuration comes from environment variables (a `.env` file is loaded if present).
See [`.env.example`](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `STRIPE_WEBHOOK_SECRET` | — | Signing secret (`whsec_…`). Without it, the endpoint fails closed with a 500. |
| `STRIPE_API_KEY` | — | Sandbox secret key, used only by `scripts/create_checkout_session.py` |
| `DATABASE_PATH` | `data/webhook_sync.db` | SQLite file |
| `ORDER_API_BASE_URL` | `http://127.0.0.1:8000/mock-oms` | Order service the worker calls |
| `ORDER_API_TIMEOUT_SECONDS` | `2` | Client timeout per call |
| `MOCK_OMS_STALL_SECONDS` | `4` | How long an injected `timeout` fault stalls |
| `MAX_ATTEMPTS` | `5` | Retry budget per cycle (a replay grants a new cycle) |
| `RETRY_BASE_SECONDS` / `RETRY_MAX_SECONDS` | `1` / `30` | Backoff base and cap |
| `WORKER_ENABLED`, `WORKER_POLL_SECONDS` | `true`, `0.5` | Background worker |
| `ENABLE_SIMULATOR` | `true` | Dashboard simulator, fault injection, and reset. Turn off outside demos. |

If you change the port, also update `ORDER_API_BASE_URL`.

## Project layout

```
app/
  main.py        app factory, starts/stops the worker in the lifespan
  config.py      Settings from environment
  db.py          SQLite schema, connections, synthetic seed data
  webhooks.py    POST /webhooks/stripe: verify → persist → 2xx
  events.py      event store: dedup insert, queries, replay
  worker.py      claim/lease, sync attempt, error classification, backoff
  oms.py         mock order service: idempotent mark-paid + fault injection
  dashboard.py   dashboard pages, replay, offline simulator endpoints
  synthetic.py   synthetic event builder + Stripe-scheme signer
  templates/     Jinja templates; static/style.css
scripts/
  simulate.py                 offline CLI (signed events over HTTP, faults, status)
  create_checkout_session.py  sandbox Checkout Session for a synthetic order
tests/           pytest suite (see table above)
```

## Scope and production notes

This is a demo, sized to read in one sitting. For production, I'd change these:

- **Dashboard auth.** The dashboard and the simulator endpoints have no authentication.
  Put them behind auth (or a VPN), and disable `ENABLE_SIMULATOR`.
- **Storage and queue.** SQLite with a polling worker thread is fine for a single node.
  For several app instances, use Postgres with `SELECT … FOR UPDATE SKIP LOCKED`, or a
  real queue. The claim, lease and fencing logic carries over unchanged.
- **Order service.** The mock order service lives in the same process for convenience,
  but the worker reaches it only over HTTP via `ORDER_API_BASE_URL`, so pointing it at a
  real service needs no code change. The real service must honor `Idempotency-Key`.
- **Payment details.** The worker checks `payment_status`. A real integration would also
  handle `checkout.session.async_payment_succeeded`, and possibly compare `amount_total`
  with the order total.
- **Events are processed independently.** Stripe doesn't guarantee delivery order, so
  each event is handled on its own merits.
