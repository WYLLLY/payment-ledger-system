"""ASGI application factory; importing the module does not open a connection."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from payment_ledger.config import Settings
from payment_ledger.database import create_database_engine
from payment_ledger.health import router


def create_app(settings: Settings | None = None) -> FastAPI:
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Engine]]:
        engine = create_database_engine(configuration)
        try:
            yield {"db_engine": engine}
        finally:
            engine.dispose()

    app = FastAPI(title="Payment & Ledger System", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app
