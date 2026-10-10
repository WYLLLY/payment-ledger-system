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
                        """SELECT current_user = 'payment_app'
                           AND has_schema_privilege(current_user, 'payment', 'USAGE')
                           AND to_regclass('payment.ledger_journals') IS NOT NULL
                           AND to_regclass('payment.ledger_entries') IS NOT NULL
                           AND has_table_privilege(
                               current_user, to_regclass('payment.ledger_journals'), 'SELECT'
                           )
                           AND has_table_privilege(
                               current_user, to_regclass('payment.ledger_entries'), 'SELECT'
                           )
                           AND to_regclass('payment.users') IS NOT NULL
                           AND to_regclass('payment.sessions') IS NOT NULL
                           AND to_regclass('payment.wallets') IS NOT NULL
                           AND to_regclass('payment.ledger_accounts') IS NOT NULL
                           AND to_regclass('payment.audit_events') IS NOT NULL
                           AND to_regclass('payment.auth_rate_limits') IS NOT NULL
                           AND has_table_privilege(
                               current_user, to_regclass('payment.users'), 'SELECT'
                           )
                           AND has_table_privilege(
                               current_user, to_regclass('payment.sessions'), 'SELECT'
                           )
                           AND has_table_privilege(
                               current_user, to_regclass('payment.wallets'), 'SELECT'
                           )
                           AND has_table_privilege(
                               current_user, to_regclass('payment.ledger_accounts'), 'SELECT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.users'), 'email', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.users'), 'password_hash', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.sessions'), 'token_hash', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.sessions'), 'user_id', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.sessions'), 'expires_at', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.audit_events'),
                               'user_id', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.audit_events'),
                               'event_type', 'INSERT'
                           )
                           AND has_column_privilege(
                               current_user, to_regclass('payment.audit_events'),
                               'resource_id', 'INSERT'
                           )
                           AND has_function_privilege(
                               current_user, to_regprocedure('payment.create_wallet(uuid,text)'),
                               'EXECUTE'
                           )
                           AND has_function_privilege(
                               current_user, to_regprocedure('payment.revoke_session(bytea)'),
                               'EXECUTE'
                           )
                           AND has_function_privilege(
                               current_user,
                               to_regprocedure('payment.take_auth_slot(bytea,integer,integer)'),
                               'EXECUTE'
                           )"""
                    )
                )
            )
    except SQLAlchemyError:
        # Database exceptions may contain hostnames, statements or credentials.
        logger.warning("Database readiness check failed")
        return False
