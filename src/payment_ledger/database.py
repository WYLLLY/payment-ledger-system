"""SQLAlchemy engine lifecycle and PostgreSQL readiness probe with timeouts."""

import logging

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from payment_ledger.config import Settings

logger = logging.getLogger(__name__)


def create_database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url(),
        isolation_level="READ COMMITTED",
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=settings.db_pool_timeout,
        hide_parameters=True,
        connect_args={
            "connect_timeout": settings.db_connect_timeout,
            "options": (
                f"-c statement_timeout={settings.db_statement_timeout_ms} "
                "-c search_path=pg_catalog,payment"
            ),
        },
    )


def database_is_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            return bool(
                connection.scalar(
                    text(
                        "SELECT current_user = 'payment_app' "
                        "AND has_schema_privilege(current_user, 'payment', 'USAGE')"
                    )
                )
            )
    except SQLAlchemyError:
        # Database exceptions may contain hostnames, statements or credentials.
        logger.warning("Database readiness check failed")
        return False
