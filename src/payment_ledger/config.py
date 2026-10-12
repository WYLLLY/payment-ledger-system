"""Typed runtime configuration; no migration credentials enter the application."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    db_host: str = Field(default="127.0.0.1", min_length=1)
    db_port: int = Field(default=15432, ge=1, le=65535)
    db_name: str = Field(default="payment_ledger", min_length=1)
    db_password: SecretStr = Field(min_length=1)
    db_connect_timeout: int = Field(default=3, ge=1, le=30)
    db_statement_timeout_ms: int = Field(default=3000, ge=100, le=30000)
    db_pool_timeout: int = Field(default=3, ge=1, le=30)
    session_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    auth_rate_limit: int = Field(default=20, ge=1, le=1000)
    auth_rate_window_seconds: int = Field(default=60, ge=1, le=3600)

    financial_rate_limit: int = Field(default=60, ge=1, le=1000)
    financial_rate_window_seconds: int = Field(default=60, ge=1, le=3600)

    def database_url(self) -> URL:
        return URL.create(
            "postgresql+psycopg",
            username="payment_app",
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )
