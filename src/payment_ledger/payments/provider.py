"""HTTP provider boundary: only matching, validated terminal evidence can release funds."""

import asyncio
import json
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, Field

from payment_ledger.schemas import Currency

Outcome = Literal["succeeded", "failed", "unknown", "not_found"]
MAX_RESPONSE_BYTES = 16384


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
        return asyncio.run(self.lookup_async(payment))

    def submit(self, payment: ProviderRequest) -> Outcome:
        return asyncio.run(self.submit_async(payment))

    async def lookup_async(self, payment: ProviderRequest) -> Outcome:
        return await self._request(payment, submit=False)

    async def submit_async(self, payment: ProviderRequest) -> Outcome:
        return await self._request(payment, submit=True)

    async def _request(self, payment: ProviderRequest, *, submit: bool) -> Outcome:
        url = self.base_url.rstrip("/") + "/payments"
        if not submit:
            url += "/" + payment.provider_key
        try:
            async with (
                asyncio.timeout(self.timeout),
                httpx2.AsyncClient(timeout=self.timeout, trust_env=False) as client,
                client.stream(
                    "POST" if submit else "GET",
                    url,
                    content=payment.model_dump_json().encode() if submit else None,
                    headers={
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                        "Idempotency-Key": payment.provider_key,
                        "X-Request-ID": str(payment.payment_id),
                    },
                ) as response,
            ):
                if response.status_code != 200 and (submit or response.status_code != 404):
                    return "unknown"
                body = bytearray()
                async for chunk in response.aiter_raw():
                    if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                        return "unknown"
                    body.extend(chunk)
                if response.status_code == 404:
                    # Only the simulator's explicit absence response permits resubmission.
                    return (
                        "not_found"
                        if json.loads(body) == {"detail": "Payment not found"}
                        else "unknown"
                    )
                result = ProviderResult.model_validate_json(body)
        except httpx2.HTTPError, httpx2.InvalidURL, TimeoutError, ValueError:
            return "unknown"

        if result.key != payment.provider_key or any(
            getattr(result, name) != getattr(payment, name) for name in ProviderRequest.model_fields
        ):
            return "unknown"
        return result.state
