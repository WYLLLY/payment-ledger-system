import pytest
from pydantic import SecretStr, ValidationError

from payment_ledger.config import Settings


def test_password_is_required() -> None:
    with pytest.raises(ValidationError):
        Settings()


def test_password_is_redacted_and_url_handles_special_characters() -> None:
    password = "test-only:@/% value"
    settings = Settings(db_password=SecretStr(password))
    assert password not in repr(settings)
    assert password not in str(settings.database_url())
    assert settings.database_url().password == password
    assert settings.database_url().username == "payment_app"


@pytest.mark.parametrize("port", [0, 65536])
def test_invalid_port_is_rejected(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password=SecretStr("test-only"), db_port=port)


def test_empty_password_is_rejected_without_exposing_input() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(db_password=SecretStr(""))
    assert "input_value" not in str(error.value)


def test_environment_is_typed_and_cannot_select_migration_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_DB_PASSWORD", "test-only")
    monkeypatch.setenv("APP_DB_PORT", "5433")
    monkeypatch.setenv("APP_DB_USER", "payment_migrator")
    settings = Settings()
    assert settings.db_port == 5433
    assert settings.database_url().username == "payment_app"
