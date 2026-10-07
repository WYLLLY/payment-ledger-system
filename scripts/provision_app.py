"""Set the runtime login password outside versioned migrations."""

import psycopg
from psycopg import sql
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from payment_ledger.config import Settings


class Credentials(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    migration_db_password: SecretStr = Field(min_length=1)


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
        # Encrypt client-side so a PostgreSQL statement log cannot reveal the password.
        encrypted = connection.pgconn.encrypt_password(
            settings.db_password.get_secret_value().encode(), b"payment_app", b"scram-sha-256"
        ).decode()
        connection.execute(
            sql.SQL("ALTER ROLE payment_app LOGIN PASSWORD {}").format(sql.Literal(encrypted))
        )


if __name__ == "__main__":
    main()
