"""Background worker: turns stored events into order updates, with bounded retries.

Lifecycle of a `checkout.session.completed` event:

    pending --claim--> processing --2xx-----------------------> succeeded
                           |      --timeout / network / 429 / 5xx --> retrying --(backoff)--> processing
                           |      --other 4xx, bad event -----------> failed
                           |      --retry budget exhausted ---------> failed
                           '------ payment_status != paid ----------> ignored

Safety properties:
* A claim is an atomic UPDATE inside BEGIN IMMEDIATE, so one event is processed by one
  worker at a time. The claim is a lease: if a worker dies, the event is reclaimed after
  `processing_lease_seconds`.
* The attempt counter doubles as a fencing token: a worker whose lease expired cannot
  overwrite the result of the worker that reclaimed the event.
* The order update itself is idempotent (Idempotency-Key = Checkout Session ID, and the
  order service only moves pending -> paid), so a retry after an ambiguous timeout,
  a redelivered event, or a manual replay can never update the same order twice.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

import httpx

from app import db
from app.config import Settings

log = logging.getLogger("webhook_sync.worker")


class OrderApiClient:
    """HTTP client for the order service."""

    def __init__(self, base_url: str, timeout: float, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.http = http or httpx.Client()

    def mark_paid(self, order_id: str, session: dict, event_id: str) -> httpx.Response:
        return self.http.post(
            f"{self.base_url}/orders/{order_id}/payments",
            json={
                "checkout_session_id": session.get("id"),
                "amount_total": session.get("amount_total"),
                "currency": session.get("currency"),
                "stripe_event_id": event_id,
            },
            # Keyed on the Checkout Session, not the event: two different events about
            # the same payment must still collapse into one order update.
            headers={"Idempotency-Key": f"checkout_session:{session.get('id')}"},
            timeout=self.timeout,
        )

    def close(self) -> None:
        self.http.close()


@dataclass
class Outcome:
    kind: str  # succeeded | retry | failed | ignored
    detail: str | None = None
    http_status: int | None = None
    retry_after: float | None = None


def _error_text(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("error") or body.get("note") or json.dumps(body)
    except ValueError:
        msg = resp.text[:200]
    return f"HTTP {resp.status_code}: {msg}"


def sync_event(row, client: OrderApiClient) -> Outcome:
    """Perform one sync attempt for a claimed event and classify the result."""
    event = json.loads(row["payload"])
    session = (event.get("data") or {}).get("object") or {}
    if not row["order_id"]:
        return Outcome("failed", "event has no order reference (metadata.order_id / client_reference_id)")
    if not session.get("id"):
        return Outcome("failed", "event has no checkout session id")
    if session.get("payment_status") != "paid":
        return Outcome("ignored", f"payment_status={session.get('payment_status')!r}; "
                                  "nothing to sync until the payment succeeds")

    try:
        resp = client.mark_paid(row["order_id"], session, row["id"])
    except httpx.TimeoutException as exc:
        return Outcome("retry", f"timeout after {client.timeout:g}s calling order service ({type(exc).__name__})")
    except httpx.TransportError as exc:
        return Outcome("retry", f"network error calling order service: {exc}")

    code = resp.status_code
    if 200 <= code < 300:
        note = None
        if resp.headers.get("Idempotent-Replayed") == "true":
            note = "order service reported the update as already applied (idempotent no-op)"
        return Outcome("succeeded", note, code)
    if code == 429 or code >= 500:
        retry_after = None
        try:
            retry_after = float(resp.headers.get("Retry-After", ""))
        except ValueError:
            pass
        return Outcome("retry", _error_text(resp), code, retry_after)
    return Outcome("failed", _error_text(resp), code)


class Worker:
    def __init__(
        self,
        settings: Settings,
        client: OrderApiClient,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
    ):
        self.settings = settings
        self.path = settings.database_path
        self.client = client
        self.clock = clock
        self.rng = rng
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- scheduling --------------------------------------------------------------

    def backoff_delay(self, cycle_attempt: int, retry_after: float | None = None) -> float:
        """Exponential backoff, capped: base * 2^(n-1), with "equal jitter" (50-100%)."""
        s = self.settings
        delay = min(s.retry_max_seconds, s.retry_base_seconds * (2 ** (cycle_attempt - 1)))
        if s.retry_jitter:
            delay = delay / 2 + (delay / 2) * self.rng()
        if retry_after is not None:
            delay = max(delay, min(retry_after, s.retry_max_seconds))
        return delay

    def claim_next(self, now: float):
        """Atomically claim the next due event. Returns (row, attempt_id) or None."""
        with db.transaction(self.path) as conn:
            row = conn.execute(
                """
                SELECT id FROM events
                WHERE (status IN ('pending', 'retrying') AND next_attempt_at <= ?)
                   OR (status = 'processing' AND locked_until < ?)
                ORDER BY next_attempt_at
                LIMIT 1
                """,
                (now, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE events SET status = 'processing', locked_until = ?,
                    attempts = attempts + 1, cycle_attempts = cycle_attempts + 1, updated_at = ?
                WHERE id = ?
                """,
                (now + self.settings.processing_lease_seconds, now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM events WHERE id = ?", (row["id"],)).fetchone()
            cur = conn.execute(
                "INSERT INTO event_attempts (event_id, attempt_no, started_at) VALUES (?, ?, ?)",
                (claimed["id"], claimed["attempts"], now),
            )
            return claimed, cur.lastrowid

    def _finish(self, row, attempt_id: int, outcome: Outcome, now: float) -> str:
        kind = outcome.kind
        error = outcome.detail if kind != "succeeded" else None
        next_attempt_at = None
        if kind == "retry":
            if row["cycle_attempts"] >= row["max_attempts"]:
                kind = "failed"
                error = f"retries exhausted after {row['cycle_attempts']} attempts; last error: {outcome.detail}"
            else:
                next_attempt_at = now + self.backoff_delay(row["cycle_attempts"], outcome.retry_after)
        status = {"retry": "retrying"}.get(kind, kind)
        terminal = status in {"succeeded", "failed", "ignored"}

        with db.transaction(self.path) as conn:
            # Fencing: only the holder of the current attempt may write the result.
            cur = conn.execute(
                """
                UPDATE events SET status = ?, next_attempt_at = ?, locked_until = NULL,
                    last_error = COALESCE(?, last_error), updated_at = ?,
                    completed_at = CASE WHEN ? THEN ? ELSE NULL END
                WHERE id = ? AND status = 'processing' AND attempts = ?
                """,
                (status, next_attempt_at, error, now, terminal, now, row["id"], row["attempts"]),
            )
            conn.execute(
                "UPDATE event_attempts SET finished_at = ?, outcome = ?, http_status = ?, error = ?"
                " WHERE id = ?",
                (now, kind, outcome.http_status,
                 outcome.detail if kind != "failed" else error, attempt_id),
            )
        if cur.rowcount == 0:
            log.warning("lost lease on %s; result of attempt %s discarded", row["id"], row["attempts"])
        return status

    # --- execution ---------------------------------------------------------------

    def run_once(self, now: float | None = None) -> str | None:
        """Process at most one due event. Returns its event ID, or None if idle."""
        claimed = self.claim_next(self.clock() if now is None else now)
        if claimed is None:
            return None
        row, attempt_id = claimed
        try:
            outcome = sync_event(row, self.client)
        except Exception as exc:  # bug or unexpected data: keep the event, retry with backoff
            log.exception("unexpected error syncing %s", row["id"])
            outcome = Outcome("retry", f"unexpected error: {exc!r}")
        status = self._finish(row, attempt_id, outcome, self.clock() if now is None else now)
        log.info("event %s attempt %d -> %s %s", row["id"], row["attempts"], status, outcome.detail or "")
        return row["id"]

    def run_until_idle(self, now: float | None = None, limit: int = 100) -> int:
        n = 0
        while n < limit and self.run_once(now) is not None:
            n += 1
        return n

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="webhook-sync-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)
        self.client.close()

    def _loop(self) -> None:
        log.info("worker started (poll every %.1fs)", self.settings.worker_poll_seconds)
        while not self._stop.is_set():
            try:
                busy = self.run_once() is not None
            except Exception:
                log.exception("worker loop error")
                busy = False
            if not busy:
                self._stop.wait(self.settings.worker_poll_seconds)
