"""Owner-scoped ledger history without exposing counterpart/system accounts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import Engine, text

from payment_ledger.errors import ResourceNotFound


class HistoryEntry(BaseModel):
    journal_id: UUID
    account_id: UUID
    account_kind: Literal["available", "reserved"]
    kind: Literal["opening"]
    currency: Literal["EUR", "USD", "JPY"]
    amount_minor: int
    created_at: datetime


def history(
    engine: Engine, user_id: UUID, wallet_id: UUID, limit: int, offset: int
) -> list[HistoryEntry]:
    with engine.connect() as connection:
        if not connection.scalar(
            text("SELECT 1 FROM payment.wallets WHERE id=:wallet AND user_id=:owner"),
            {"wallet": wallet_id, "owner": user_id},
        ):
            raise ResourceNotFound
        rows = connection.execute(
            text("""
                SELECT e.journal_id, e.account_id, a.account_kind, j.kind,
                       e.currency, e.amount_minor, j.created_at
                FROM payment.ledger_entries e
                JOIN payment.ledger_journals j ON j.id = e.journal_id
                JOIN payment.ledger_accounts a ON a.id = e.account_id
                WHERE a.wallet_id = :wallet
                ORDER BY j.created_at DESC, j.id DESC, a.id
                LIMIT :limit OFFSET :offset
            """),
            {"wallet": wallet_id, "limit": limit, "offset": offset},
        )
        return [HistoryEntry.model_validate(row) for row in rows.mappings()]
