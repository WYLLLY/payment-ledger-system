"""Each test owns a fresh database on a dedicated development/test cluster.

Runtime roles are cluster-wide, as in PostgreSQL itself. The Compose migration
service provisions their credentials before this suite runs. No tests are skipped
when PostgreSQL or credentials are absent: missing infrastructure is a failure.
"""

import os
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from payment_ledger.config import Settings

ROOT = Path(__file__).resolve().parents[2]
MIGRATION_TIMEOUT_SECONDS = 30


class TestCredentials(BaseSettings):
    __test__ = False
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", hide_input_in_errors=True
    )
    postgres_password: SecretStr = Field(min_length=1)
    migration_db_password: SecretStr = Field(min_length=1)


def _run_migration(
    settings: Settings, credentials: TestCredentials, direction: str, revision: str
) -> tuple[int, str]:
    """Keep the raw child environment and output out of failure traceback frames."""
    environment = {
        **os.environ,
        "APP_DB_HOST": settings.db_host,
        "APP_DB_PORT": str(settings.db_port),
        "APP_DB_NAME": settings.db_name,
        "MIGRATION_DB_PASSWORD": credentials.migration_db_password.get_secret_value(),
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", direction, revision],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=False,
            timeout=MIGRATION_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, f"Alembic timed out after {MIGRATION_TIMEOUT_SECONDS} seconds"
    except OSError:
        return 127, "Alembic process could not be started"

    diagnostic = result.stderr.decode("utf-8", errors="replace")
    for password in (
        settings.db_password,
        credentials.postgres_password,
        credentials.migration_db_password,
    ):
        diagnostic = diagnostic.replace(password.get_secret_value(), "[REDACTED]")
    return result.returncode, diagnostic.strip()


@dataclass(frozen=True)
class Database:
    settings: Settings
    credentials: TestCredentials

    def connect(
        self, *, user: str = "payment_app", dbname: str | None = None, autocommit: bool = False
    ) -> psycopg.Connection[tuple[object, ...]]:
        password = self.settings.db_password
        if user == "payment_migrator":
            password = self.credentials.migration_db_password
        elif user == "postgres":
            password = self.credentials.postgres_password
        database_name = dbname if dbname is not None else self.settings.db_name
        try:
            return psycopg.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                dbname=database_name,
                user=user,
                password=password.get_secret_value(),
                autocommit=autocommit,
                connect_timeout=3,
            )
        except psycopg.Error as error:
            reason = "authentication failed" if error.sqlstate == "28P01" else "unavailable"
        # Raise outside the handler: the original psycopg frames contain raw conninfo.
        raise RuntimeError(
            f"PostgreSQL {reason}: {self.settings.db_host}:{self.settings.db_port}/"
            f"{database_name} as {user}"
        ) from None

    def migrate(self, direction: str, revision: str) -> None:
        returncode, diagnostic = _run_migration(
            self.settings, self.credentials, direction, revision
        )
        if returncode:
            raise RuntimeError(
                f"Alembic {direction} {revision} failed (exit {returncode}): {diagnostic}"
            ) from None


@pytest.fixture
def database() -> Iterator[Database]:
    settings = Settings()
    credentials = TestCredentials()
    name = f"payment_test_{uuid4().hex}"
    bootstrap = Database(settings, credentials)
    with bootstrap.connect(user="postgres", dbname="postgres", autocommit=True) as administrator:
        administrator.execute(
            sql.SQL("CREATE DATABASE {} OWNER payment_migrator").format(sql.Identifier(name))
        )
        try:
            test_database = Database(settings.model_copy(update={"db_name": name}), credentials)
            test_database.migrate("upgrade", "head")
            yield test_database
        finally:
            administrator.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )
