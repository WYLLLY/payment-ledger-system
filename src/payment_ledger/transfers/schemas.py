from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from payment_ledger.schemas import Currency


class TransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    source_wallet_id: UUID
    destination_wallet_id: UUID
    currency: Currency
    amount_minor: int = Field(strict=True, ge=1, le=1000000000000)


class TransferResponse(BaseModel):
    id: UUID
    source_wallet_id: UUID
    destination_wallet_id: UUID
    currency: Currency
    amount_minor: int
    created_at: datetime
    journal_id: UUID
