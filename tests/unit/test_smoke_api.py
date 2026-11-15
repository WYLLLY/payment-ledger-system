"""Both local smoke flows must use the port selected for Compose."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import Request

import pytest

from scripts import smoke_api


@dataclass
class Response:
    status: int = 200

    def read(self) -> bytes:
        return b'{"status":"ok"}'


@pytest.mark.parametrize("from_environment", [False, True])
def test_smoke_requests_use_configured_api_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, from_environment: bool
) -> None:
    monkeypatch.delenv("API_PORT", raising=False)
    (tmp_path / ".env").write_text("API_PORT=18001\n")
    expected = 18002 if from_environment else 18001
    if from_environment:
        monkeypatch.setenv("API_PORT", str(expected))

    @contextmanager
    def respond(request: Request, *, timeout: float) -> Iterator[Response]:
        assert request.full_url == f"http://127.0.0.1:{expected}/health/ready"
        assert timeout == 10
        yield Response()

    monkeypatch.setattr(smoke_api, "urlopen", respond)
    assert smoke_api.call("/health/ready") == {"status": "ok"}
