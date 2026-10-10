import hashlib

import pytest
from argon2 import extract_parameters
from argon2.low_level import Type
from pydantic import SecretStr, ValidationError

from payment_ledger.schemas import RegistrationRequest
from payment_ledger.security import hash_password, new_session_token, token_digest, verify_password


def test_argon2id_uses_random_salts_and_rejects_wrong_or_unknown_password() -> None:
    password = SecretStr("test-only-password-123")
    first, second = hash_password(password), hash_password(password)
    assert first != second
    parameters = extract_parameters(first)
    assert parameters.type is Type.ID
    assert parameters.memory_cost == 65536
    assert parameters.time_cost == 3
    assert verify_password(password, first)
    assert not verify_password(SecretStr("wrong"), first)
    assert not verify_password(password, None)
    assert password.get_secret_value() not in first


def test_session_tokens_are_random_and_stored_as_sha256() -> None:
    tokens = {new_session_token() for _ in range(100)}
    assert len(tokens) == 100
    for token in tokens:
        assert len(token) == 43
        assert token_digest(token) == hashlib.sha256(token.encode("ascii")).digest()
    for invalid in ("", "a" * 42, "a" * 44, "é" * 43, "!" * 43):
        assert token_digest(invalid) is None


def test_registration_normalizes_email_and_hides_password_repr() -> None:
    payload = RegistrationRequest(email="Alice@EXAMPLE.COM", password="long-enough-password")
    assert payload.email == "alice@example.com"
    assert "long-enough-password" not in repr(payload)


@pytest.mark.parametrize("password", ["short", "x" * 129])
def test_registration_enforces_password_bounds(password: str) -> None:
    with pytest.raises(ValidationError):
        RegistrationRequest(email="a@example.com", password=password)
