"""Explicit dev/test-only fixed fixtures. Never run in a demonstration deployment.

Provision the NOLOGIN fixture role only here, not in Alembic or Compose startup.
Use migration credentials solely for local provisioning. Actual openings execute
as payment_dev; runtime API and worker cannot assume this role.
"""

import os
from uuid import UUID

import psycopg
from psycopg import sql
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from payment_ledger.config import Settings
from payment_ledger.security import hash_password


class Credentials(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_db_password: SecretStr = Field(min_length=1)


DEMO_USERS = (
    (UUID("00000000-0000-4000-8000-000000000001"), "alice.demo@example.com"),
    (UUID("00000000-0000-4000-8000-000000000002"), "bob.demo@example.com"),
)


def provision_dev_role(connection: psycopg.Connection[tuple[object, ...]]) -> None:
    connection.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'payment_dev') THEN
                CREATE ROLE payment_dev NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                    NOINHERIT NOREPLICATION NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT FROM pg_roles r WHERE rolname = 'payment_dev' AND (
                rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole OR rolinherit
                OR rolreplication OR rolbypassrls OR EXISTS (
                    SELECT FROM pg_auth_members WHERE member = r.oid
                )
            )) OR EXISTS (
                SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
                JOIN pg_roles granted ON granted.oid = m.roleid
                WHERE granted.rolname = 'payment_dev' AND r.rolname <> 'payment_migrator'
            ) THEN
                RAISE EXCEPTION 'unsafe dev role attributes or memberships';
            END IF;
        END $$;
        GRANT USAGE ON SCHEMA payment TO payment_dev;
        GRANT EXECUTE ON FUNCTION payment.open_fixture_wallet(uuid, uuid) TO payment_dev;
    """)


def seed(connection: psycopg.Connection[tuple[object, ...]], password_hash: str) -> None:
    # One local transaction creates all identities, wallets, openings and their audits.
    # Serialize seed runs before any account locks; reruns leave existing fixtures intact.
    connection.execute("SELECT pg_advisory_xact_lock(73003)")
    pending: list[tuple[UUID, UUID]] = []
    for user_id, email in DEMO_USERS:
        connection.execute(
            "INSERT INTO payment.users(id,email,password_hash) VALUES (%s,%s,%s) "
            "ON CONFLICT (id) DO NOTHING",
            (user_id, email, password_hash),
        )
        if connection.execute(
            "SELECT email FROM payment.users WHERE id=%s", (user_id,)
        ).fetchone() != (email,):
            raise RuntimeError("Fixture identity collision")
        for currency in ("EUR", "USD", "JPY"):
            wallet = connection.execute(
                "SELECT id FROM payment.wallets WHERE user_id=%s AND currency=%s",
                (user_id, currency),
            ).fetchone()
            if wallet is None:
                wallet = connection.execute(
                    "SELECT payment.create_wallet(%s,%s)", (user_id, currency)
                ).fetchone()
            assert wallet is not None
            if connection.execute(
                "SELECT 1 FROM payment.ledger_journals WHERE opening_wallet_id=%s", (wallet[0],)
            ).fetchone():
                continue
            pending.append((user_id, UUID(str(wallet[0]))))
    # A batch is one transaction: acquire the union of its account locks in global
    # UUID order before any posting. Per-pair ordering alone can deadlock batches.
    account_ids = connection.execute(
        "SELECT id FROM payment.ledger_accounts "
        "WHERE (wallet_id=ANY(%s::uuid[]) AND account_kind='available') "
        "OR (wallet_id IS NULL AND account_kind='funding') ORDER BY id",
        ([target for _, target in pending],),
    ).fetchall()
    for (account_id,) in account_ids:
        connection.execute(
            "SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE", (account_id,)
        )
    for user_id, target in pending:
        connection.execute("SET LOCAL ROLE payment_dev")
        connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", (user_id, target))
        connection.execute("RESET ROLE")


def main() -> int:
    if os.environ.get("PAYMENT_LEDGER_DEV_FIXTURES") != "1":
        print("Set PAYMENT_LEDGER_DEV_FIXTURES=1 only on a dedicated dev/test cluster.")
        return 1
    password = os.environ.get("DEMO_PASSWORD", "")
    if not 12 <= len(password) <= 128:
        print("Supply DEMO_PASSWORD (12–128 characters) for the fixed demo identities.")
        return 1
    hashed = hash_password(SecretStr(password))
    settings = Settings()
    credentials = Credentials()
    try:
        with psycopg.connect(
            host=settings.db_host,
            port=settings.db_port,
            dbname=settings.db_name,
            user="payment_migrator",
            password=credentials.migration_db_password.get_secret_value(),
            connect_timeout=3,
        ) as connection:
            provision_dev_role(connection)
            # PG17 grants role creator ADMIN/SET membership; ensure SET for reused roles.
            connection.execute("GRANT payment_dev TO payment_migrator WITH SET TRUE")
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO payment_dev").format(
                    sql.Identifier(settings.db_name)
                )
            )
            seed(connection, hashed)
    except psycopg.Error, RuntimeError:
        print("Fixture provisioning failed; transaction rolled back (credentials redacted).")
        return 1
    print("Fixed fixtures ready: two users, EUR/USD/JPY wallets; reruns preserve balances.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
