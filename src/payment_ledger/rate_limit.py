"""Durable per-user admission counters, independent of the financial transaction."""

from typing import Literal
from uuid import UUID

from sqlalchemy import Engine, text

from payment_ledger.errors import RateLimitExceeded
from payment_ledger.payments.service import transaction


class FinancialRateLimitExceeded(RateLimitExceeded):
    pass


def enforce(
    engine: Engine,
    owner: UUID,
    kind: Literal["transfer", "payment"],
    key: str,
    limit: int,
    window: int,
) -> None:
    retry = transaction(
        engine,
        lambda connection: connection.scalar(
            text("SELECT payment.take_financial_slot(:owner,:kind,:key,:limit,:window)"),
            {"owner": owner, "kind": kind, "key": key, "limit": limit, "window": window},
        ),
    )
    if retry:
        raise FinancialRateLimitExceeded(int(retry))
