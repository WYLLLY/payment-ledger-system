import io
import json
import logging
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from payment_ledger.observability import JsonFormatter, RequestLogging, correlation_id


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("payment_ledger.events")
    old = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old)


def test_logs_and_correlation_exclude_sensitive_request_data(log_stream: io.StringIO) -> None:
    app = FastAPI()
    app.add_middleware(RequestLogging)

    @app.get("/secret/{identifier}")
    def route(identifier: str) -> dict[str, str]:
        return {"status": "ok"}

    with TestClient(app) as client:
        identifier = str(uuid4())
        response = client.get(
            "/secret/private-path?token=private-query",
            headers={"Authorization": "Bearer private-token", "X-Request-ID": identifier},
        )
        assert response.headers["X-Request-ID"] == identifier
        bad = client.get("/missing-private", headers={"X-Request-ID": "private-untrusted-id"})
        UUID(bad.headers["X-Request-ID"])
        assert bad.headers["X-Request-ID"] != identifier
    logs = log_stream.getvalue()
    assert "private" not in logs
    first, second = [json.loads(line) for line in logs.splitlines()]
    assert first["correlation_id"] == identifier and first["route"] == "/secret/{identifier}"
    assert first["status"] == 200 and second["status"] == 404
    assert second["route"] == "unmatched"
    assert correlation_id.get() is None


def test_unhandled_error_is_generic_and_json_logs_hide_exception(log_stream: io.StringIO) -> None:
    app = FastAPI()
    app.add_middleware(RequestLogging)

    @app.get("/failure")
    def failure() -> None:
        raise RuntimeError("private-password")

    with TestClient(app) as client:
        response = client.get("/failure")
    assert response.status_code == 500 and response.json() == {"detail": "Internal error"}
    assert "private-password" not in log_stream.getvalue()
    assert all(json.loads(line) for line in log_stream.getvalue().splitlines())


def test_formatter_ignores_arbitrary_extras_and_exception_text() -> None:
    record = logging.LogRecord(
        "payment_ledger", logging.ERROR, "", 0, "database_unavailable", (), None
    )
    record.authorization = "private-token"
    record.sql = "private-query"
    assert "private" not in JsonFormatter().format(record)
