"""Explicit local transactions; retry only errors guaranteeing transaction abort.

Never retry connection loss/commit ambiguity. Client replay through the same key
retrieves the durable result, even if the original HTTP response was lost.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from payment_ledger.errors import ResourceNotFound
from payment_ledger.transfers.schemas import TransferRequest, TransferResponse

MAX_TRANSACTION_ATTEMPTS = 3
RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: dict[str, object]


def create_transfer(
    engine: Engine, owner: UUID, key: str, request: TransferRequest
) -> StoredResponse:
    for attempt in range(MAX_TRANSACTION_ATTEMPTS):
        try:
            with engine.begin() as connection:
                row = connection.execute(
                    text(
                        "SELECT * FROM payment.create_transfer("
                        ":owner,:key,:source,:destination,CAST(:amount AS bigint),:currency)"
                    ),
                    {
                        "owner": owner,
                        "key": key,
                        "source": request.source_wallet_id,
                        "destination": request.destination_wallet_id,
                        "amount": request.amount_minor,
                        "currency": request.currency,
                    },
                ).one()
                result = StoredResponse(status_code=int(row.status_code), body=dict(row.body))
                if result.status_code == 201:
                    # Validate before commit; return the stored representation unchanged.
                    TransferResponse.model_validate(result.body)
            return result
        except DBAPIError as error:
            if (
                getattr(error.orig, "sqlstate", None) not in RETRYABLE_SQLSTATES
                or attempt + 1 == MAX_TRANSACTION_ATTEMPTS
            ):
                raise
    raise AssertionError("unreachable retry state")


TRANSFER_SELECT = """
    SELECT t.id,t.source_wallet_id,t.destination_wallet_id,t.currency,t.amount_minor,
           t.created_at,j.id AS journal_id
    FROM payment.transfers t JOIN payment.ledger_journals j ON j.transfer_id=t.id
    WHERE t.user_id=:owner
"""


def get_transfer(engine: Engine, owner: UUID, transfer_id: UUID) -> TransferResponse:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(TRANSFER_SELECT + " AND t.id=:id"), {"owner": owner, "id": transfer_id}
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound
        return TransferResponse.model_validate(row)


def list_transfers(engine: Engine, owner: UUID, limit: int, offset: int) -> list[TransferResponse]:
    with engine.connect() as connection:
        return [
            TransferResponse.model_validate(row)
            for row in connection.execute(
                text(
                    TRANSFER_SELECT
                    + " ORDER BY t.created_at DESC,t.id DESC LIMIT :limit OFFSET :offset"
                ),
                {"owner": owner, "limit": limit, "offset": offset},
            ).mappings()
        ]
