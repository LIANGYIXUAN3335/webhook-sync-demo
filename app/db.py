"""SQLite storage: schema, connections, and synthetic seed data.

Each unit of work opens its own short-lived connection. WAL mode plus a busy timeout
lets the HTTP handlers and the background worker thread share one database file.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

SCHEMA = """
-- One row per Stripe event ID. The primary key is the deduplication boundary.
CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    type                TEXT NOT NULL,
    livemode            INTEGER NOT NULL DEFAULT 0,
    payload             TEXT NOT NULL,           -- raw, signature-verified request body
    checkout_session_id TEXT,
    order_id            TEXT,
    -- pending -> processing -> succeeded
    --                       -> retrying -> processing ...
    --                       -> failed (permanent error or retries exhausted)
    -- ignored: verified but nothing to sync (other event types, unpaid sessions)
    status              TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,  -- total attempts, all cycles
    cycle_attempts      INTEGER NOT NULL DEFAULT 0,  -- attempts since last (re)enqueue
    max_attempts        INTEGER NOT NULL,
    next_attempt_at     REAL,
    locked_until        REAL,
    last_error          TEXT,
    delivery_count      INTEGER NOT NULL DEFAULT 1,  -- how often Stripe delivered it
    replay_count        INTEGER NOT NULL DEFAULT 0,  -- manual replays from the dashboard
    received_at         REAL NOT NULL,
    last_delivery_at    REAL NOT NULL,
    updated_at          REAL NOT NULL,
    completed_at        REAL
);
CREATE INDEX IF NOT EXISTS idx_events_due ON events(status, next_attempt_at);

-- Append-only log of every sync attempt, for the dashboard detail view.
CREATE TABLE IF NOT EXISTS event_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL REFERENCES events(id),
    attempt_no   INTEGER NOT NULL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    outcome      TEXT,        -- succeeded | retry | failed | ignored
    http_status  INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_event ON event_attempts(event_id);

-- Synthetic orders owned by the (mock) order management service.
CREATE TABLE IF NOT EXISTS orders (
    id                  TEXT PRIMARY KEY,
    customer_name       TEXT NOT NULL,
    customer_email      TEXT NOT NULL,
    amount_cents        INTEGER NOT NULL,
    currency            TEXT NOT NULL,
    status              TEXT NOT NULL,       -- pending | paid
    paid_session_id     TEXT UNIQUE,
    paid_amount_cents   INTEGER,
    paid_at             REAL,
    update_count        INTEGER NOT NULL DEFAULT 0,  -- must never exceed 1 per order
    created_at          REAL NOT NULL
);

-- Idempotency keys seen by the mock order service.
CREATE TABLE IF NOT EXISTS oms_idempotency (
    key            TEXT PRIMARY KEY,
    order_id       TEXT NOT NULL,
    status_code    INTEGER NOT NULL,
    response_json  TEXT NOT NULL,
    created_at     REAL NOT NULL
);

-- Queue of injected faults for the mock order service (demo/testing only).
CREATE TABLE IF NOT EXISTS oms_faults (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mode       TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

SYNTHETIC_ORDERS = [
    # (id, name, email, amount_cents, currency) -- all fictional.
    ("ord_demo_1001", "Demo Buyer One", "buyer1@example.test", 2500, "usd"),
    ("ord_demo_1002", "Demo Buyer Two", "buyer2@example.test", 4999, "usd"),
    ("ord_demo_1003", "Demo Buyer Three", "buyer3@example.test", 1200, "usd"),
    ("ord_demo_1004", "Demo Buyer Four", "buyer4@example.test", 8800, "usd"),
    ("ord_demo_1005", "Demo Buyer Five", "buyer5@example.test", 3000, "usd"),
    ("ord_demo_1006", "Demo Buyer Six", "buyer6@example.test", 1999, "usd"),
]


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(path: str) -> Iterator[sqlite3.Connection]:
    """Autocommit connection, closed on exit."""
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(path: str, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """A write transaction. BEGIN IMMEDIATE takes the write lock up front, so
    read-then-write sequences (claiming an event, applying a payment) are atomic."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


def init_db(path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with session(path) as conn:
        conn.executescript(SCHEMA)
    seed_orders(path)


def seed_orders(path: str) -> None:
    now = time.time()
    with transaction(path) as conn:
        for oid, name, email, amount, currency in SYNTHETIC_ORDERS:
            conn.execute(
                "INSERT OR IGNORE INTO orders (id, customer_name, customer_email, amount_cents,"
                " currency, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (oid, name, email, amount, currency, now),
            )


def reset_demo_data(path: str) -> None:
    """Wipe events, orders, and faults, then re-seed the synthetic orders."""
    with transaction(path) as conn:
        for table in ("event_attempts", "events", "oms_idempotency", "oms_faults", "orders"):
            conn.execute(f"DELETE FROM {table}")
    seed_orders(path)
