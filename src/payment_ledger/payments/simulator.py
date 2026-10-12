"""Fictional provider. SQLite transactions persist one immutable result before HTTP response."""

import asyncio
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, Header, HTTPException

from payment_ledger.observability import RequestLogging, configure_logging
from payment_ledger.payments.provider import ProviderRequest

Mode = Literal["success", "decline", "timeout_before", "timeout_after", "unavailable"]


class ProviderStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection, connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS outcomes (
                    key TEXT PRIMARY KEY,
                    payment_id TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('succeeded','failed'))
                );
                CREATE TRIGGER IF NOT EXISTS immutable_outcome_update BEFORE UPDATE ON outcomes
                BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_outcome_delete BEFORE DELETE ON outcomes
                BEGIN SELECT RAISE(ABORT,'immutable outcome'); END;
            """)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def lookup(self, key: str) -> dict[str, object] | None:
        with closing(self.connect()) as connection, connection:
            row = connection.execute(
                "SELECT payload,state FROM outcomes WHERE key=?", (key,)
            ).fetchone()
        if row is None:
            return None
        payment = ProviderRequest.model_validate_json(row[0])
        return {**payment.model_dump(mode="json"), "key": key, "state": row[1]}

    def submit(self, key: str, payment: ProviderRequest, *, decline: bool) -> dict[str, object]:
        if key != payment.provider_key:
            raise HTTPException(409, "Provider identity conflict")
        payload = payment.model_dump_json()
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT payload,state FROM outcomes WHERE key=?", (key,)
            ).fetchone()
            if row is not None and row[0] != payload:
                raise HTTPException(409, "Provider parameters conflict")
            state = row[1] if row is not None else ("failed" if decline else "succeeded")
            if row is None:
                connection.execute(
                    "INSERT INTO outcomes VALUES(?,?,?,?)",
                    (key, str(payment.payment_id), payload, state),
                )
        return {**payment.model_dump(mode="json"), "key": key, "state": state}


def create_simulator(path: Path, *, mode: Mode = "success", delay: float = 5.0) -> FastAPI:
    store = ProviderStore(path)
    configure_logging()
    app = FastAPI(title="Fictional payment provider")
    app.add_middleware(RequestLogging)

    @app.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/payments/{key}")
    async def lookup(key: str) -> dict[str, object]:
        if mode == "unavailable":
            raise HTTPException(503, "Provider unavailable")
        result = await asyncio.to_thread(store.lookup, key)
        if result is None:
            raise HTTPException(404, "Payment not found")
        return result

    @app.post("/payments")
    async def submit(
        payment: ProviderRequest, idempotency_key: Annotated[str, Header()]
    ) -> dict[str, object]:
        if mode == "unavailable":
            raise HTTPException(503, "Provider unavailable")
        if mode == "timeout_before":
            await asyncio.sleep(delay)
            raise HTTPException(503, "Provider unavailable")
        result = await asyncio.to_thread(
            store.submit, idempotency_key, payment, decline=mode == "decline"
        )
        if mode == "timeout_after":
            await asyncio.sleep(delay)
        return result

    return app


def create_app() -> FastAPI:
    # Fault modes are injected only by tests; the deployed simulator succeeds by default.
    return create_simulator(Path(os.environ.get("PROVIDER_DB_PATH", ".local/provider.sqlite3")))
