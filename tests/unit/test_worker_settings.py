"""Validate that the worker has time to finalize after provider I/O."""

import pytest
from pydantic import ValidationError

from payment_ledger.payments.worker import WorkerSettings


def test_worker_rejects_lease_without_finalization_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WORKER_DB_PASSWORD", "test-only")
    monkeypatch.setenv("WORKER_HTTP_TIMEOUT", "5")
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "6")
    with pytest.raises(ValidationError, match="finalization"):
        WorkerSettings()
    monkeypatch.setenv("WORKER_LEASE_SECONDS", "30")
    assert WorkerSettings().lease_seconds == 30
