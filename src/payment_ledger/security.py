"""Password verification and cryptographic session identity; no HTTP or database IO."""

import hashlib
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type
from pydantic import SecretStr

PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, salt_len=16, type=Type.ID
)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def hash_password(password: SecretStr) -> str:
    return PASSWORD_HASHER.hash(password.get_secret_value())


def verify_password(password: SecretStr, stored_hash: str | None) -> bool:
    try:
        PASSWORD_HASHER.verify(stored_hash or DUMMY_PASSWORD_HASH, password.get_secret_value())
    except VerifyMismatchError:
        return False
    return stored_hash is not None


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> bytes | None:
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        return None
    return hashlib.sha256(token.encode("ascii")).digest()
