"""Local transactions return only after commit; ambiguous commits are never replayed."""

from collections.abc import Callable
from uuid import UUID

from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import DBAPIError

from payment_ledger.errors import ResourceNotFound
from payment_ledger.payments.schemas import PaymentRequest, PaymentResponse
from payment_ledger.transfers.service import (
    MAX_TRANSACTION_ATTEMPTS,
    RETRYABLE_SQLSTATES,
    StoredResponse,
)


def transaction[T](engine: Engine, operation: Callable[[Connection], T]) -> T:
    for attempt in range(MAX_TRANSACTION_ATTEMPTS):
        try:
            with engine.begin() as connection:
                result = operation(connection)
            return result
        except DBAPIError as error:
            if (
                getattr(error.orig, "sqlstate", None) not in RETRYABLE_SQLSTATES
                or attempt + 1 == MAX_TRANSACTION_ATTEMPTS
            ):
                raise
    raise AssertionError("unreachable retry state")


def create_payment(
    engine: Engine, owner: UUID, key: str, request: PaymentRequest
) -> StoredResponse:
    def create(connection: Connection) -> StoredResponse:
        row = connection.execute(
            text(
                "SELECT * FROM payment.create_payment("
                ":owner,:key,:wallet_id,:recipient,CAST(:amount_minor AS bigint),:currency)"
            ),
            {"owner": owner, "key": key, **request.model_dump()},
        ).one()
        result = StoredResponse(status_code=int(row.status_code), body=dict(row.body))
        if result.status_code == 202:
            PaymentResponse.model_validate(result.body)
        return result

    return transaction(engine, create)


PAYMENT_SELECT = """
    SELECT id,wallet_id,recipient,currency,amount_minor,state,created_at,updated_at
    FROM payment.payments WHERE user_id=:owner
"""


def get_payment(engine: Engine, owner: UUID, payment_id: UUID) -> PaymentResponse:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(PAYMENT_SELECT + " AND id=:id"), {"owner": owner, "id": payment_id}
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound
        return PaymentResponse.model_validate(row)


def list_payments(engine: Engine, owner: UUID, limit: int, offset: int) -> list[PaymentResponse]:
    with engine.connect() as connection:
        return [
            PaymentResponse.model_validate(row)
            for row in connection.execute(
                text(
                    PAYMENT_SELECT + " ORDER BY created_at DESC,id DESC LIMIT :limit OFFSET :offset"
                ),
                {"owner": owner, "limit": limit, "offset": offset},
            ).mappings()
        ]
