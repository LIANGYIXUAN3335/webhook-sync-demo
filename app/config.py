"""Runtime configuration, read from environment variables (optionally via .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    stripe_api_key: str = ""
    stripe_webhook_secret: str = ""
    database_path: str = "data/webhook_sync.db"
    order_api_base_url: str = "http://127.0.0.1:8000/mock-oms"
    order_api_timeout_seconds: float = 2.0
    mock_oms_stall_seconds: float = 4.0
    worker_enabled: bool = True
    worker_poll_seconds: float = 0.5
    max_attempts: int = 5
    retry_base_seconds: float = 1.0
    retry_max_seconds: float = 30.0
    # Seconds a "processing" claim is held before another worker may reclaim it
    # (covers a worker crashing mid-attempt).
    processing_lease_seconds: float = 30.0
    enable_simulator: bool = True
    # Random jitter on backoff; tests turn it off for deterministic timings.
    retry_jitter: bool = True

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        env = os.environ.get
        return cls(
            stripe_api_key=env("STRIPE_API_KEY", ""),
            stripe_webhook_secret=env("STRIPE_WEBHOOK_SECRET", ""),
            database_path=env("DATABASE_PATH", "data/webhook_sync.db"),
            order_api_base_url=env("ORDER_API_BASE_URL", "http://127.0.0.1:8000/mock-oms"),
            order_api_timeout_seconds=float(env("ORDER_API_TIMEOUT_SECONDS", "2")),
            mock_oms_stall_seconds=float(env("MOCK_OMS_STALL_SECONDS", "4")),
            worker_enabled=_bool(env("WORKER_ENABLED"), True),
            worker_poll_seconds=float(env("WORKER_POLL_SECONDS", "0.5")),
            max_attempts=int(env("MAX_ATTEMPTS", "5")),
            retry_base_seconds=float(env("RETRY_BASE_SECONDS", "1")),
            retry_max_seconds=float(env("RETRY_MAX_SECONDS", "30")),
            enable_simulator=_bool(env("ENABLE_SIMULATOR"), True),
        )
