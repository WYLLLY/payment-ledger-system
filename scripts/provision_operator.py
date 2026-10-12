"""Explicit, optional provisioning of the separate internal operator login."""

import psycopg
from psycopg import sql
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from payment_ledger.operations.cli import OperatorSettings


class MigrationSecret(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_db_password: SecretStr = Field(min_length=1)


def provision_operator(
    connection: psycopg.Connection[tuple[object, ...]], password: SecretStr
) -> None:
    connection.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='payment_operator_login') THEN
                CREATE ROLE payment_operator_login LOGIN INHERIT NOSUPERUSER NOCREATEDB
                    NOCREATEROLE NOREPLICATION NOBYPASSRLS;
            END IF;
            IF EXISTS (SELECT FROM pg_roles r WHERE r.rolname='payment_operator_login' AND
                (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication
                 OR r.rolbypassrls OR NOT r.rolinherit OR EXISTS (
                    SELECT FROM pg_auth_members m JOIN pg_roles granted ON granted.oid=m.roleid
                    WHERE m.member=r.oid AND (granted.rolname<>'payment_operator'
                                             OR m.admin_option)))) THEN
                RAISE EXCEPTION 'Unsafe operator login';
            END IF;
        END $$;
        GRANT payment_operator TO payment_operator_login;
    """)
    encrypted = connection.pgconn.encrypt_password(
        password.get_secret_value().encode(), b"payment_operator_login", b"scram-sha-256"
    ).decode()
    connection.execute(
        sql.SQL("ALTER ROLE payment_operator_login LOGIN PASSWORD {}").format(
            sql.Literal(encrypted)
        )
    )


def main() -> None:
    settings = OperatorSettings()
    credentials = MigrationSecret()
    with psycopg.connect(
        host=settings.db_host,
        port=settings.db_port,
        dbname=settings.db_name,
        user="payment_migrator",
        connect_timeout=3,
        password=credentials.migration_db_password.get_secret_value(),
    ) as connection:
        provision_operator(connection, settings.db_password)


if __name__ == "__main__":
    main()
