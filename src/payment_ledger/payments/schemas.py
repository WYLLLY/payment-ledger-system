from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from payment_ledger.schemas import Currency


class PaymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    wallet_id: UUID
    recipient: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:@-]+$")
    currency: Currency
    amount_minor: int = Field(strict=True, ge=1, le=1000000000000)


class PaymentResponse(PaymentRequest):
    id: UUID
    state: Literal["pending", "needs_review", "succeeded", "failed"]
    created_at: datetime
    updated_at: datetime
