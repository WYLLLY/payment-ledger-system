"""ASGI application factory; importing the module does not open a connection."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import Engine

from payment_ledger.api import install_error_handlers
from payment_ledger.api import router as identity_router
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
    app.state.settings = configuration
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(identity_router)
    return app
