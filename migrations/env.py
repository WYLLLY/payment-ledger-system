"""Migrations require separate credentials and never use runtime configuration."""

from alembic import context
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL, create_engine, pool


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    app_db_host: str = "127.0.0.1"
    app_db_port: int = Field(default=15432, ge=1, le=65535)
    app_db_name: str = "payment_ledger"
    migration_db_password: SecretStr = Field(min_length=1)


if context.is_offline_mode():
    raise RuntimeError("Role and database validation require online migrations")

settings = MigrationSettings()
url = URL.create(
    "postgresql+psycopg",
    username="payment_migrator",
    password=settings.migration_db_password.get_secret_value(),
    host=settings.app_db_host,
    port=settings.app_db_port,
    database=settings.app_db_name,
)
engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
try:
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=None, version_table_schema="public"
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
