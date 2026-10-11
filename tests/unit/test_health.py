from fastapi.testclient import TestClient
from pydantic import SecretStr

from payment_ledger.config import Settings
from payment_ledger.main import create_app


def test_liveness_does_not_require_a_database_connection() -> None:
    settings = Settings(db_host="invalid.invalid", db_password=SecretStr("test-only"))
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_documents_unavailable_response() -> None:
    with TestClient(create_app(Settings(db_password=SecretStr("test-only")))) as client:
        schema = client.get("/openapi.json").json()
    assert "503" in schema["paths"]["/health/ready"]["get"]["responses"]
    assert set(schema["paths"]) == {
        "/health/live",
        "/health/ready",
        "/auth/register",
        "/auth/login",
        "/auth/logout",
        "/users/me",
        "/wallets",
        "/wallets/{wallet_id}",
        "/wallets/{wallet_id}/ledger",
        "/transfers",
        "/transfers/{transfer_id}",
    }
