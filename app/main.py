"""FastAPI application factory.

Run with: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from app import dashboard, db, oms, webhooks
from app.config import Settings
from app.worker import OrderApiClient, Worker


def _configure_logging() -> None:
    """Send the app's own loggers (webhook_sync.*) to stderr next to uvicorn's logs."""
    logger = logging.getLogger("webhook_sync")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    _configure_logging()
    db.init_db(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = None
        if settings.worker_enabled:
            client = OrderApiClient(settings.order_api_base_url, settings.order_api_timeout_seconds)
            worker = Worker(settings, client)
            worker.start()
        app.state.worker = worker
        try:
            yield
        finally:
            if worker:
                await run_in_threadpool(worker.stop)

    app = FastAPI(title="Webhook Sync Demo", lifespan=lifespan)
    app.state.settings = settings
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(webhooks.router)
    app.include_router(oms.router)
    app.include_router(dashboard.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app
