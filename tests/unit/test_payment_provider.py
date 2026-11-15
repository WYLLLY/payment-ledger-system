"""Untrusted HTTP evidence must be complete and match the immutable payment identity."""

import json
from collections.abc import AsyncIterator
from uuid import uuid4

import httpx2
import pytest

from payment_ledger.payments.provider import ProviderClient, ProviderRequest


@pytest.mark.parametrize(
    "fault",
    [
        "id",
        "recipient",
        "amount",
        "currency",
        "key",
        "state",
        "extra",
        "malformed",
        "oversize",
        "truncated",
        "bad_status",
        "truncated_404",
    ],
)
def test_malformed_or_mismatched_provider_evidence_is_unknown(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    payment = ProviderRequest(
        payment_id=uuid4(), recipient="vendor", amount_minor=10, currency="EUR"
    )
    fields: dict[str, object] = {
        **payment.model_dump(mode="json"),
        "key": payment.provider_key,
        "state": "succeeded",
    }
    mutations = {
        "id": ("payment_id", str(uuid4())),
        "recipient": ("recipient", "other"),
        "amount": ("amount_minor", 11),
        "currency": ("currency", "JPY"),
        "key": ("key", "other"),
        "state": ("state", "pending"),
        "extra": ("extra", True),
    }
    if fault in mutations:
        key, value = mutations[fault]
        fields[key] = value
    body = json.dumps(fields).encode()
    if fault == "malformed":
        body = b"not-json"
    elif fault == "oversize":
        body = b" " * 17000 + body

    class BrokenBody(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"partial"
            raise httpx2.RemoteProtocolError("response closed early")

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.extensions["timeout"]["read"] == 3
        if fault == "bad_status":
            raise httpx2.RemoteProtocolError("malformed HTTP status")
        if fault == "truncated_404":
            return httpx2.Response(404, stream=BrokenBody())
        if fault == "truncated":
            return httpx2.Response(200, stream=BrokenBody())
        return httpx2.Response(200, stream=httpx2.ByteStream(body))

    real_client = httpx2.AsyncClient

    def make_client(*, timeout: float, trust_env: bool) -> httpx2.AsyncClient:
        assert trust_env is False
        return real_client(
            timeout=timeout, trust_env=trust_env, transport=httpx2.MockTransport(respond)
        )

    monkeypatch.setattr("payment_ledger.payments.provider.httpx2.AsyncClient", make_client)
    provider = ProviderClient("http://provider.invalid")
    assert provider.lookup(payment) == "unknown"
    assert provider.submit(payment) == "unknown"
