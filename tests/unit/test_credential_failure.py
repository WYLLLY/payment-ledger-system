"""Regression for leaked psycopg conninfo in pytest's local-variable display."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from payment_ledger.config import Settings
from tests.integration.conftest import Database, TestCredentials


def test_unreachable_database_failure_is_visible_without_passwords() -> None:
    root = Path(__file__).resolve().parents[2]
    passwords = {
        "POSTGRES_PASSWORD": "dummy-admin-credential-5c7d",
        "MIGRATION_DB_PASSWORD": "dummy-migrator-credential-0a4e",
        "APP_DB_PASSWORD": "dummy-app-credential-8f12",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_foundation.py::test_fresh_migration_creates_only_foundation",
            "--showlocals",
            "-q",
        ],
        cwd=root,
        env={**os.environ, **passwords, "APP_DB_HOST": "127.0.0.1", "APP_DB_PORT": "1"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "PostgreSQL unavailable: 127.0.0.1:1/postgres as postgres" in output
    assert "RuntimeError" in output
    for password in passwords.values():
        assert password not in output


def test_migration_failure_redacts_child_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    app_password = "dummy-app-credential-7b30"
    admin_password = "dummy-admin-credential-72f1"
    migrator_password = "dummy-migrator-credential-611d"
    database = Database(
        Settings(db_password=SecretStr(app_password)),
        TestCredentials(
            postgres_password=SecretStr(admin_password),
            migration_db_password=SecretStr(migrator_password),
        ),
    )

    def failed_alembic(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args=["alembic"],
            returncode=1,
            stderr=(
                b"Could not connect as payment_migrator to payment_ledger; invalid byte=\xff; "
                + f"passwords={app_password},{admin_password},{migrator_password}".encode()
            ),
        )

    monkeypatch.setattr("tests.integration.conftest.subprocess.run", failed_alembic)
    with pytest.raises(RuntimeError) as failure:
        database.migrate("upgrade", "head")
    message = str(failure.value)
    assert "Alembic upgrade head failed (exit 1)" in message
    assert "Could not connect as payment_migrator to payment_ledger" in message
    assert "invalid byte=\ufffd" in message
    assert message.count("[REDACTED]") == 3
    for password in (app_password, admin_password, migrator_password):
        assert password not in message


def test_malformed_migration_failure_under_showlocals(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    failing_test = tmp_path / "test_malformed_child.py"
    failing_test.write_text(
        """
import os
import subprocess

from pydantic import SecretStr
from payment_ledger.config import Settings
from tests.integration.conftest import Database, TestCredentials


def failed_alembic(*_args, **_kwargs):
    return subprocess.CompletedProcess(
        args=["alembic"],
        returncode=1,
        stderr=(
            b"PostgreSQL connection failed; invalid byte=\\xff; password="
            + os.environ["MIGRATION_DB_PASSWORD"].encode()
        ),
    )


def test_migration_failure(monkeypatch):
    monkeypatch.setattr("tests.integration.conftest.subprocess.run", failed_alembic)
    database = Database(
        Settings(db_password=SecretStr(os.environ["APP_DB_PASSWORD"])),
        TestCredentials(
            postgres_password=SecretStr(os.environ["POSTGRES_PASSWORD"]),
            migration_db_password=SecretStr(os.environ["MIGRATION_DB_PASSWORD"]),
        ),
    )
    database.migrate("upgrade", "head")
""".lstrip()
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(failing_test),
            "--showlocals",
            "-q",
        ],
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(root),
            "APP_DB_PASSWORD": "dummy-app-credential-7b30",
            "POSTGRES_PASSWORD": "dummy-admin-credential-72f1",
            "MIGRATION_DB_PASSWORD": "dummy-migrator-credential-611d",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Alembic upgrade head failed (exit 1)" in output
    assert "PostgreSQL connection failed; invalid byte=\ufffd" in output
    assert "[REDACTED]" in output
    for password in (
        "dummy-app-credential-7b30",
        "dummy-admin-credential-72f1",
        "dummy-migrator-credential-611d",
    ):
        assert password not in output
