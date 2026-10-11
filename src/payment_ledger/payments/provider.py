"""HTTP provider boundary: only matching, validated terminal evidence can release funds."""

import json
from dataclasses import dataclass
from http.client import HTTPException
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payment_ledger.schemas import Currency

Outcome = Literal["succeeded", "failed", "unknown", "not_found"]


class ProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    payment_id: UUID
    recipient: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.:@-]+$")
    amount_minor: int = Field(strict=True, ge=1, le=1000000000000)
    currency: Currency

    @property
    def provider_key(self) -> str:
        return f"payment:{self.payment_id}"


class ProviderResult(ProviderRequest):
    key: str
    state: Literal["succeeded", "failed"]


@dataclass(frozen=True)
class ProviderClient:
    base_url: str
    timeout: float = 3.0

    def lookup(self, payment: ProviderRequest) -> Outcome:
        return self._request(payment, submit=False)

    def submit(self, payment: ProviderRequest) -> Outcome:
        return self._request(payment, submit=True)

    def _request(self, payment: ProviderRequest, *, submit: bool) -> Outcome:
        url = self.base_url.rstrip("/") + "/payments"
        data = None
        if submit:
            data = payment.model_dump_json().encode()
        else:
            url += "/" + payment.provider_key
        request = Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Idempotency-Key": payment.provider_key,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                if response.status != 200:
                    return "unknown"
                body = response.read(16385)
                if len(body) > 16384:
                    return "unknown"
            result = ProviderResult.model_validate_json(body)
            if result.key != payment.provider_key or any(
                getattr(result, name) != getattr(payment, name)
                for name in ProviderRequest.model_fields
            ):
                return "unknown"
            return result.state
        except HTTPError as error:
            try:
                # Only the simulator's explicit absence response permits resubmission.
                if not submit and error.code == 404:
                    body = json.loads(error.read(16384))
                    if body == {"detail": "Payment not found"}:
                        return "not_found"
            except ValueError, OSError, HTTPException:
                pass
            finally:
                error.close()
            return "unknown"
        except URLError, OSError, HTTPException, ValidationError, ValueError:
            return "unknown"
