"""FastAPI application factory.

Run with: uvicorn app.main:create_app --factory
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db, webhooks
from app.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    db.init_db(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield

    app = FastAPI(title="Webhook Sync Demo", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(webhooks.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    return app

