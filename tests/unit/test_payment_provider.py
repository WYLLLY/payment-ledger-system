"""Untrusted HTTP evidence must be complete and match the immutable payment identity."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

import pytest

from payment_ledger.payments.provider import ProviderClient, ProviderRequest


@dataclass
class Response:
    body: bytes
    status: int = 200

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


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

    @contextmanager
    def respond(request: Request, *, timeout: float) -> Iterator[Response]:
        assert timeout == 3
        if fault == "truncated":
            raise IncompleteRead(b"partial")
        if fault == "bad_status":
            raise BadStatusLine("broken")
        if fault == "truncated_404":

            class TruncatedError(HTTPError):
                def read(self, n: int = -1) -> bytes:
                    raise IncompleteRead(b"partial")

            raise TruncatedError(request.full_url, 404, "not found", Message(), None)
        yield Response(body)

    monkeypatch.setattr("payment_ledger.payments.provider.urlopen", respond)
    provider = ProviderClient("http://provider.invalid")
    assert provider.lookup(payment) == "unknown"
    assert provider.submit(payment) == "unknown"
