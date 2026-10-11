"""Set the runtime login password outside versioned migrations."""

import psycopg
from psycopg import sql
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from payment_ledger.config import Settings


class Credentials(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_db_password: SecretStr = Field(min_length=1)
    worker_db_password: SecretStr = Field(min_length=1)


def provision_worker(
    connection: psycopg.Connection[tuple[object, ...]], password: SecretStr
) -> None:
    connection.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='payment_worker_login') THEN
                CREATE ROLE payment_worker_login LOGIN INHERIT NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT FROM pg_roles r WHERE r.rolname='payment_worker_login' AND
                (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR
                    r.rolbypassrls
                 OR NOT r.rolinherit OR EXISTS (
                    SELECT FROM pg_auth_members m JOIN pg_roles granted ON granted.oid=m.roleid
                    WHERE m.member=r.oid AND (granted.rolname<>'payment_worker' OR
                        m.admin_option))))
            THEN RAISE EXCEPTION 'Unsafe worker login'; END IF;
        END $$;
        GRANT payment_worker TO payment_worker_login;
    """)
    encrypted = connection.pgconn.encrypt_password(
        password.get_secret_value().encode(), b"payment_worker_login", b"scram-sha-256"
    ).decode()
    connection.execute(
        sql.SQL("ALTER ROLE payment_worker_login LOGIN PASSWORD {}").format(sql.Literal(encrypted))
    )


def main() -> None:
    settings = Settings()
    credentials = Credentials()
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user="payment_migrator",
        password=credentials.migration_db_password.get_secret_value(),
        connect_timeout=3,
    ) as connection:
        provision_worker(connection, credentials.worker_db_password)
        # Encrypt client-side so a PostgreSQL statement log cannot reveal the password.
        encrypted = connection.pgconn.encrypt_password(
            settings.db_password.get_secret_value().encode(), b"payment_app", b"scram-sha-256"
        ).decode()
        connection.execute(
            sql.SQL("ALTER ROLE payment_app LOGIN PASSWORD {}").format(sql.Literal(encrypted))
        )


if __name__ == "__main__":
    main()
