"""Asynchronous outbox worker; each local transaction closes before network I/O.

Every claim first queries provider state, including recovery from a crash before
or after submission. Five claims at most may submit. needs_review claims only
query; they never infer failure from absence and never resubmit.
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, Connection, Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from payment_ledger.payments.provider import ProviderClient, ProviderRequest
from payment_ledger.payments.service import transaction

logger = logging.getLogger(__name__)


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="WORKER_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=15432, ge=1, le=65535)
    db_name: str = "payment_ledger"
    db_password: SecretStr = Field(min_length=1)
    provider_url: str = "http://127.0.0.1:8001"
    http_timeout: float = Field(default=3.0, gt=0, le=10)
    lease_seconds: int = Field(default=30, ge=1, le=300)
    poll_seconds: float = Field(default=1, gt=0, le=60)

    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username="payment_worker_login",
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )


def create_worker_engine(settings: WorkerSettings) -> Engine:
    return create_engine(
        settings.database_url(),
        isolation_level="READ COMMITTED",
        hide_parameters=True,
        pool_pre_ping=True,
        pool_timeout=3,
        connect_args={"connect_timeout": 3, "options": "-c statement_timeout=3000"},
    )


@dataclass(frozen=True)
class Claim:
    payment: ProviderRequest
    generation: int
    attempts: int
    state: str


def claim(engine: Engine, lease_seconds: int = 30) -> Claim | None:
    def operation(connection: Connection) -> Claim | None:
        row = (
            connection.execute(
                text("SELECT * FROM payment.claim_payment(:lease)"), {"lease": lease_seconds}
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return Claim(
            ProviderRequest.model_validate(
                {name: row[name] for name in ProviderRequest.model_fields}
            ),
            int(row["generation"]),
            int(row["attempts"]),
            str(row["state"]),
        )

    return transaction(engine, operation)


def finish(
    engine: Engine,
    payment_id: UUID,
    generation: int,
    outcome: Literal["succeeded", "failed", "unknown"],
) -> bool:
    return transaction(
        engine,
        lambda connection: bool(
            connection.scalar(
                text("SELECT payment.finish_payment(:id,CAST(:generation AS bigint),:outcome)"),
                {"id": payment_id, "generation": generation, "outcome": outcome},
            )
        ),
    )


async def run_once(engine: Engine, provider: ProviderClient, lease_seconds: int = 30) -> bool:
    item = await asyncio.to_thread(claim, engine, lease_seconds)
    if item is None:
        return False
    outcome = await asyncio.to_thread(provider.lookup, item.payment)
    if outcome == "not_found" and item.state == "pending":
        outcome = await asyncio.to_thread(provider.submit, item.payment)
    final = outcome if outcome in ("succeeded", "failed") else "unknown"
    await asyncio.to_thread(finish, engine, item.payment.payment_id, item.generation, final)
    return True


async def serve(settings: WorkerSettings) -> None:
    engine = create_worker_engine(settings)
    provider = ProviderClient(settings.provider_url, settings.http_timeout)
    try:
        while True:
            try:
                worked = await run_once(engine, provider, settings.lease_seconds)
            except SQLAlchemyError:
                # Includes ambiguous commits: wait, then claim again through the durable outbox.
                # Never re-run HTTP or a committed local transaction directly.
                logger.warning("Payment worker database operation unavailable")
                worked = False
            if not worked:
                await asyncio.sleep(settings.poll_seconds)
    finally:
        engine.dispose()


def main() -> None:
    asyncio.run(serve(WorkerSettings()))


if __name__ == "__main__":
    main()
