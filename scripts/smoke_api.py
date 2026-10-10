"""Exercise the local Compose API; leaves two dummy users and one empty wallet."""

import json
import secrets
from typing import cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


def call(
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, str] | None = None,
    token: str | None = None,
    expected: int = 200,
) -> dict[str, object]:
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(
        f"http://127.0.0.1:8000{path}",
        method=method,
        headers=headers,
        data=json.dumps(payload).encode() if payload is not None else None,
    )
    try:
        with urlopen(request, timeout=10) as response:
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
        error.close()
    if status != expected:
        raise RuntimeError(f"{method} {path}: expected {expected}, received {status}")
    return cast(dict[str, object], json.loads(body)) if body else {}


def main() -> None:
    call("/health/live")
    call("/health/ready")
    tokens = []
    for _ in range(2):
        credentials = {
            "email": f"smoke-{uuid4().hex}@example.com",
            "password": secrets.token_urlsafe(24),
        }
        user = call("/auth/register", method="POST", payload=credentials, expected=201)
        session = call("/auth/login", method="POST", payload=credentials)
        token = str(session["access_token"])
        tokens.append(token)
        assert call("/users/me", token=token)["id"] == user["id"]
    wallet = call(
        "/wallets", method="POST", payload={"currency": "EUR"}, token=tokens[0], expected=201
    )
    assert call(f"/wallets/{wallet['id']}", token=tokens[0]) == wallet
    call(f"/wallets/{wallet['id']}", token=tokens[1], expected=404)
    for token in tokens:
        call("/auth/logout", method="POST", token=token, expected=204)
        call("/users/me", token=token, expected=401)
    print("PASS: Compose health, registration, login, wallet, ownership and logout")


if __name__ == "__main__":
    main()
