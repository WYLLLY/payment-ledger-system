"""M6 admission, diagnostics and operator recovery on real PostgreSQL and HTTP."""

import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi import FastAPI
from psycopg.errors import CheckViolation, InsufficientPrivilege, InvalidParameterValue
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.exc import DBAPIError

from payment_ledger.database import create_database_engine
from payment_ledger.operations import service as ops
from payment_ledger.operations.cli import OperatorSettings, create_operator_engine
from payment_ledger.payments import worker
from payment_ledger.payments.provider import Outcome, ProviderRequest
from payment_ledger.payments.simulator import Mode
from scripts.provision_operator import provision_operator

from .conftest import Database
from .test_ledger import assert_clean
from .test_payments import amounts, counts, live_provider, make_due, payload, post, provider_request
from .test_payments import payment_scenario as payment_scenario
from .test_payments import worker_engine as worker_engine
from .test_transfers import Scenario

pytestmark = pytest.mark.integration


@pytest.fixture
def operator_engine(database: Database) -> Iterator[Engine]:
    def connect() -> psycopg.Connection[tuple[object, ...]]:
        connection = database.connect(user="postgres")
        connection.execute("SET SESSION AUTHORIZATION payment_operator")
        connection.commit()
        return connection

    engine = create_engine(
        "postgresql+psycopg://",
        creator=connect,
        hide_parameters=True,
        isolation_level="READ COMMITTED",
    )
    try:
        yield engine
    finally:
        engine.dispose()


def test_financial_limit_replay_conflict_and_rollover(scenario: Scenario) -> None:
    s = scenario
    cast(FastAPI, s.client.app).state.settings.financial_rate_limit = 1
    original = post(s)
    headers = {**s.alice_headers, "Idempotency-Key": "second"}
    denied = s.client.post("/payments", json=payload(s), headers=headers)
    assert denied.status_code == 429
    assert 1 <= int(denied.headers["Retry-After"]) <= 60
    assert post(s) == original
    conflict = s.client.post(
        "/payments", json=payload(s, 2000), headers={**s.alice_headers, "Idempotency-Key": "pay-1"}
    )
    assert conflict.status_code == 409
    assert counts(s) == (1, 1, 1)
    with s.database.connect(user="payment_migrator") as c:
        assert c.execute("SELECT count(*) FROM payment.idempotency_records").fetchone() == (1,)
        c.execute("UPDATE payment.financial_rate_limits SET window_start=0,attempts=1001")
    assert s.client.post("/payments", json=payload(s), headers=headers).status_code == 202
    assert amounts(s) == (98000, 2000, 0)
    assert_clean(s.database)


@pytest.mark.parametrize("path", ["/payments", "/transfers"])
def test_concurrent_admission_has_exact_bound(scenario: Scenario, path: str) -> None:
    s = scenario
    cast(FastAPI, s.client.app).state.settings.financial_rate_limit = 3
    # A future window keeps the test deterministic even across the wall-clock boundary.
    with s.database.connect(user="payment_migrator") as c:
        c.execute(
            "INSERT INTO payment.financial_rate_limits VALUES (%s,%s,"
            "floor(extract(epoch FROM clock_timestamp()))::bigint+3600,1)",
            (s.alice, "payment" if path == "/payments" else "transfer"),
        )
    barrier = Barrier(8)
    body = (
        payload(s)
        if path == "/payments"
        else {
            "source_wallet_id": str(s.source),
            "destination_wallet_id": str(s.destination),
            "amount_minor": 1000,
            "currency": "EUR",
        }
    )

    def request(index: int) -> int:
        barrier.wait(timeout=5)
        return s.client.post(
            path, json=body, headers={**s.alice_headers, "Idempotency-Key": f"concurrent-{index}"}
        ).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(request, range(8)))
    assert statuses.count(202 if path == "/payments" else 201) == 2
    assert statuses.count(429) == 6
    assert_clean(s.database)


def test_rate_scope_and_failures_are_counted(scenario: Scenario) -> None:
    s = scenario
    cast(FastAPI, s.client.app).state.settings.financial_rate_limit = 1
    denied = s.client.post(
        "/payments",
        json=payload(s, 100001),
        headers={**s.alice_headers, "Idempotency-Key": "insufficient"},
    )
    assert denied.status_code == 409
    assert (
        s.client.post(
            "/payments", json=payload(s), headers={**s.alice_headers, "Idempotency-Key": "new"}
        ).status_code
        == 429
    )
    transfer = {
        "source_wallet_id": str(s.source),
        "destination_wallet_id": str(s.destination),
        "amount_minor": 1,
        "currency": "EUR",
    }
    assert (
        s.client.post(
            "/transfers", json=transfer, headers={**s.alice_headers, "Idempotency-Key": "new"}
        ).status_code
        == 201
    )
    # Bob has his own quota; an ownership error must not become Alice's 429.
    assert (
        s.client.post(
            "/payments", json=payload(s), headers={**s.bob_headers, "Idempotency-Key": "new"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "kind,limit,window",
    [
        (None, 1, 60),
        ("bad", 1, 60),
        ("payment", 0, 60),
        ("transfer", 1001, 60),
        ("payment", 1, 0),
        ("payment", None, 60),
    ],
)
def test_rate_function_rejects_invalid_parameters(
    scenario: Scenario, kind: str | None, limit: int | None, window: int
) -> None:
    with scenario.database.connect() as c, pytest.raises(InvalidParameterValue):
        c.execute(
            "SELECT payment.take_financial_slot(%s,%s,%s,%s,%s)",
            (scenario.alice, kind, "key", limit, window),
        )


def test_snapshot_detects_corruption_without_repair(
    scenario: Scenario, operator_engine: Engine
) -> None:
    p = post(scenario)
    before = ops.snapshot(operator_engine)
    assert before["issues"] == []
    assert before["metrics"]["pending"] == 1
    with scenario.database.connect(user="payment_migrator") as c:
        c.execute("UPDATE payment.payment_outbox SET done=true WHERE payment_id=%s", (p["id"],))
    report = ops.snapshot(operator_engine)
    assert {"issue": "payment_outbox_mismatch", "reference": p["id"]} in report["issues"]
    assert counts(scenario) == (1, 1, 1)
    with scenario.database.connect(user="payment_migrator") as c:
        assert c.execute("SELECT done FROM payment.payment_outbox").fetchone() == (True,)
        c.execute("UPDATE payment.payment_outbox SET done=false")
    assert ops.snapshot(operator_engine)["issues"] == []


def test_metrics_empty_then_expired_and_needs_review(
    scenario: Scenario, operator_engine: Engine, worker_engine: Engine
) -> None:
    assert ops.snapshot(operator_engine)["metrics"] == {
        "pending": 0,
        "needs_review": 0,
        "oldest_outbox_age_seconds": 0,
        "expired_leases": 0,
    }
    post(scenario)
    item = worker.claim(worker_engine)
    assert item
    make_due(scenario.database, expire=True)
    assert ops.snapshot(operator_engine)["metrics"]["expired_leases"] == 1
    for _ in range(5):
        make_due(scenario.database, expire=True)
        item = worker.claim(worker_engine)
        assert item
        assert worker.finish(worker_engine, item.payment.payment_id, item.generation, "unknown")
    metrics = ops.snapshot(operator_engine)["metrics"]
    assert metrics["pending"] == 0 and metrics["needs_review"] == 1
    assert metrics["oldest_outbox_age_seconds"] >= 0
    assert amounts(scenario) == (99000, 1000, 0)


def test_operator_cannot_steal_lease_and_actions_are_atomic(
    scenario: Scenario, operator_engine: Engine, worker_engine: Engine
) -> None:
    p = post(scenario)
    item = worker.claim(worker_engine)
    assert item
    assert ops.action(operator_engine, UUID(str(p["id"])), "retry", "INC-42", uuid4()) == "busy"
    assert worker.finish(worker_engine, item.payment.payment_id, item.generation, "succeeded")
    assert (
        ops.action(operator_engine, item.payment.payment_id, "retry", "INC-42", uuid4())
        == "terminal"
    )
    with operator_engine.connect() as c:
        assert [
            tuple(row)
            for row in c.execute(
                text("SELECT actor,action,result FROM payment.operator_events ORDER BY occurred_at")
            )
        ] == [
            ("payment_operator", "retry", "busy"),
            ("payment_operator", "retry", "terminal"),
        ]
    assert amounts(scenario) == (99000, 0, 1000)


def test_operator_rollback_and_repeat_after_lost_response(
    scenario: Scenario, operator_engine: Engine, worker_engine: Engine
) -> None:
    p = post(scenario)
    identifier = UUID(str(p["id"]))
    with scenario.database.connect(user="payment_migrator") as c:
        c.execute("UPDATE payment.payment_outbox SET next_attempt_at='2099-01-01'")
    with operator_engine.connect() as connection:
        transaction = connection.begin()
        assert (
            connection.scalar(
                text("SELECT payment.operator_action(:id,'retry','INC-1',:corr)"),
                {"id": identifier, "corr": uuid4()},
            )
            == "scheduled"
        )
        pid = connection.scalar(text("SELECT pg_backend_pid()"))
        with scenario.database.connect(user="postgres", autocommit=True) as c:
            c.execute("SELECT pg_terminate_backend(%s)", (pid,))
        with pytest.raises(DBAPIError):
            transaction.rollback()
    with operator_engine.connect() as c:
        assert c.scalar(text("SELECT count(*) FROM payment.operator_events")) == 0
        assert c.scalar(text("SELECT next_attempt_at>now() FROM payment.payment_outbox")) is True
    for _ in range(2):
        assert ops.action(operator_engine, identifier, "retry", "INC-1", uuid4()) == "scheduled"
    item = worker.claim(worker_engine)
    assert item and item.attempts == 1
    assert worker.finish(worker_engine, identifier, item.generation, "succeeded")
    assert counts(scenario) == (1, 1, 2)
    assert_clean(scenario.database)


@pytest.mark.parametrize("mode", ["success", "decline"])
def test_recovery_needs_review_uses_remote_evidence_only(
    scenario: Scenario,
    operator_engine: Engine,
    worker_engine: Engine,
    tmp_path: Path,
    mode: Mode,
) -> None:
    p = post(scenario)
    for _ in range(5):
        make_due(scenario.database)
        item = worker.claim(worker_engine)
        assert item
        assert worker.finish(worker_engine, item.payment.payment_id, item.generation, "unknown")
    identifier = UUID(str(p["id"]))
    with live_provider(tmp_path / "provider.db", mode=mode) as provider:
        assert ops.lookup(operator_engine, provider, identifier, "INC-42", uuid4()) == "not_found"
        assert ops.action(operator_engine, identifier, "retry", "INC-42", uuid4()) == "scheduled"
        assert asyncio.run(worker.run_once(worker_engine, provider))
        assert provider.lookup(provider_request(p)) == "not_found"
        assert amounts(scenario) == (99000, 1000, 0)
        # Remote evidence arrives later. Operator still cannot choose an outcome.
        assert provider.submit(provider_request(p)) in ("succeeded", "failed")
        assert ops.action(operator_engine, identifier, "retry", "INC-42", uuid4()) == "scheduled"
        assert asyncio.run(worker.run_once(worker_engine, provider))
        assert ops.reconcile_external(operator_engine, provider)["findings"] == []
    assert amounts(scenario) == ((99000, 0, 1000) if mode == "success" else (100000, 0, 0))
    assert counts(scenario) == (1, 1, 2)
    assert ops.snapshot(operator_engine)["issues"] == []


def test_external_report_pagination_and_no_http_transaction(
    scenario: Scenario, operator_engine: Engine, tmp_path: Path
) -> None:
    for index in range(2):
        post(scenario, f"pay-{index}")
    checked_out: list[object] = []
    event.listen(operator_engine, "checkout", lambda *args: checked_out.append(1))
    event.listen(operator_engine, "checkin", lambda *args: checked_out.pop())

    class ObservedProvider:
        def lookup(self, payment: ProviderRequest) -> Outcome:
            assert checked_out == []
            return "unknown"

    report = ops.reconcile_external(operator_engine, ObservedProvider(), limit=1)
    assert report["complete"] is False and report["checked"] == 1
    assert report["findings"][0]["issue"] == "provider_unavailable"
    with live_provider(tmp_path / "provider.db") as provider:
        second = ops.reconcile_external(
            operator_engine, provider, after=UUID(str(report["next_cursor"])), limit=1
        )
        assert second["complete"] is True and second["checked"] == 1
        assert second["findings"][0]["issue"] == "unresolved_reservation"
    assert amounts(scenario) == (98000, 2000, 0)


@pytest.mark.parametrize("role", ["payment_app", "payment_worker", "payment_operator"])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE payment.ledger_accounts SET balance_minor=0",
        "DELETE FROM payment.ledger_entries",
        "TRUNCATE payment.ledger_journals",
        "UPDATE payment.payment_outbox SET attempts=0",
        "DELETE FROM payment.operator_events",
        "INSERT INTO payment.financial_rate_limits VALUES (gen_random_uuid(),'payment',0,1)",
    ],
)
def test_operations_privilege_boundaries(database: Database, role: str, statement: str) -> None:
    with database.connect(user="postgres") as c:
        c.execute(f"SET SESSION AUTHORIZATION {role}")
        with pytest.raises(InsufficientPrivilege):
            c.execute(statement)


@pytest.mark.parametrize("role", ["payment_app", "payment_worker"])
def test_runtime_cannot_operate(database: Database, role: str) -> None:
    with database.connect(user="postgres") as c:
        c.execute(f"SET SESSION AUTHORIZATION {role}")
        with pytest.raises(InsufficientPrivilege):
            c.execute(
                "SELECT payment.operator_action(gen_random_uuid(),'retry','INC',gen_random_uuid())"
            )


def test_operator_cannot_finalize_or_read_credentials(
    database: Database, operator_engine: Engine
) -> None:
    for statement in [
        "SELECT payment.finish_payment(gen_random_uuid(),1,'succeeded')",
        "SELECT * FROM payment.sessions",
        "SELECT * FROM payment.users",
        "SET ROLE payment_worker",
        "SET ROLE payment_migrator",
    ]:
        with operator_engine.connect() as c, pytest.raises(DBAPIError) as error:
            c.execute(text(statement))
        assert getattr(error.value.orig, "sqlstate", None) == "42501"


def test_operator_audit_immutable_and_downgrade_preserved(
    scenario: Scenario, operator_engine: Engine
) -> None:
    p = post(scenario)
    ops.action(operator_engine, UUID(str(p["id"])), "retry", "INC", uuid4())
    for statement in [
        "DELETE FROM payment.operator_events",
        "TRUNCATE payment.operator_events",
        "UPDATE payment.operator_events SET reason='changed'",
    ]:
        with scenario.database.connect(user="payment_migrator") as c, pytest.raises(CheckViolation):
            c.execute(statement)
    with pytest.raises(RuntimeError, match="retained operator audit"):
        scenario.database.migrate("downgrade", "0005_payments")


@pytest.mark.migration_revision("0005_payments")
def test_m5_upgrade_and_operator_login(database: Database) -> None:
    engine = create_database_engine(database.settings)
    engine.dispose()
    database.migrate("upgrade", "head")
    secret = SecretStr(uuid4().hex)
    with database.connect(user="payment_migrator") as c:
        provision_operator(c, secret)
    settings = OperatorSettings(
        db_host=database.settings.db_host,
        db_port=database.settings.db_port,
        db_name=database.settings.db_name,
        db_password=secret,
    )
    operator = create_operator_engine(settings)
    try:
        with operator.connect() as c:
            assert c.scalar(text("SELECT current_user")) == "payment_operator_login"
        assert ops.snapshot(operator)["issues"] == []
    finally:
        operator.dispose()
    database.migrate("downgrade", "0005_payments")
    database.migrate("upgrade", "head")


def test_reconciliation_distinguishes_remote_mismatch_and_concurrent_progress(
    scenario: Scenario, operator_engine: Engine, worker_engine: Engine, tmp_path: Path
) -> None:
    p = post(scenario)
    item = worker.claim(worker_engine)
    assert item
    with live_provider(tmp_path / "provider.db") as provider:
        assert provider.submit(provider_request(p)) == "succeeded"
        assert ops.reconcile_external(operator_engine, provider)["findings"] == [
            {"issue": "awaiting_finalization", "reference": p["id"]}
        ]

        class FinishingProvider:
            def lookup(self, payment: ProviderRequest) -> Outcome:
                assert item is not None
                result = provider.lookup(payment)
                assert worker.finish(worker_engine, payment.payment_id, item.generation, "failed")
                return result

        report = ops.reconcile_external(operator_engine, FinishingProvider())
        assert report["findings"] == [{"issue": "concurrent_progress", "reference": p["id"]}]
        # Inject a contradictory trusted-worker outcome; reconciliation reports, never repairs.
        report = ops.reconcile_external(operator_engine, provider)
        assert report["findings"] == [{"issue": "provider_mismatch", "reference": p["id"]}]
        assert amounts(scenario) == (100000, 0, 0)
    assert ops.snapshot(operator_engine)["issues"] == []


def test_operator_and_claim_contend_without_resetting_attempts(
    scenario: Scenario, operator_engine: Engine, worker_engine: Engine
) -> None:
    from .test_payments import wait_for_blockers

    p = post(scenario)
    identifier = UUID(str(p["id"]))
    # Hold the exact queue lock; operator waits and SKIP LOCKED worker skips it.
    with scenario.database.connect(user="payment_migrator") as blocker:
        blocker.execute(
            "SELECT * FROM payment.payment_outbox WHERE payment_id=%s FOR UPDATE", (identifier,)
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(ops.action, operator_engine, identifier, "retry", "INC-1", uuid4())
            wait_for_blockers(scenario.database, "operator_action")
            assert worker.claim(worker_engine) is None
            blocker.commit()
            assert future.result(timeout=5) == "scheduled"
    old = worker.claim(worker_engine)
    assert old
    make_due(scenario.database, expire=True)
    assert ops.action(operator_engine, identifier, "retry", "INC-1", uuid4()) == "scheduled"
    new = worker.claim(worker_engine)
    assert new and new.generation > old.generation and new.attempts == old.attempts + 1
    assert worker.finish(worker_engine, identifier, old.generation, "failed") is False
    assert worker.finish(worker_engine, identifier, new.generation, "succeeded") is True
    assert counts(scenario) == (1, 1, 2)
    assert_clean(scenario.database)


def test_ledger_and_reservation_discrepancy_is_reported_without_changes(
    scenario: Scenario, operator_engine: Engine
) -> None:
    post(scenario)
    with scenario.database.connect(user="postgres") as c:
        c.execute("ALTER TABLE payment.ledger_accounts DISABLE TRIGGER ALL")
        c.execute(
            "UPDATE payment.ledger_accounts SET balance_minor=balance_minor+1 "
            "WHERE wallet_id=%s AND account_kind='reserved'",
            (scenario.source,),
        )
        c.execute("ALTER TABLE payment.ledger_accounts ENABLE TRIGGER ALL")
    issues = {i["issue"] for i in ops.snapshot(operator_engine)["issues"]}
    assert {"reserved_balance_mismatch", "balance_mismatch", "currency_not_conserved"} <= issues
    assert amounts(scenario) == (99000, 1001, 0)
    assert counts(scenario) == (1, 1, 1)


def test_correlation_is_audited_without_leaking_to_next_request(scenario: Scenario) -> None:
    request_id = str(uuid4())
    response = scenario.client.post(
        "/payments",
        json=payload(scenario),
        headers={**scenario.alice_headers, "Idempotency-Key": "trace", "X-Request-ID": request_id},
    )
    assert response.status_code == 202 and response.headers["X-Request-ID"] == request_id
    with scenario.database.connect(user="payment_migrator") as c:
        assert c.execute(
            "SELECT correlation_id FROM payment.audit_events WHERE event_type='payment_created'"
        ).fetchone() == (UUID(request_id),)
    post(scenario, "trace2")
    with scenario.database.connect(user="payment_migrator") as c:
        rows = c.execute(
            "SELECT correlation_id FROM payment.audit_events WHERE event_type='payment_created'"
        ).fetchall()
        assert len({r[0] for r in rows}) == 2


@pytest.mark.parametrize(
    "action,reason",
    [
        ("succeeded", "INC"),
        ("failed", "INC"),
        ("retry", ""),
        ("retry", "secret\nforged"),
        ("retry", "x" * 201),
    ],
)
def test_operator_rejects_unsafe_actions_and_reasons(
    scenario: Scenario, operator_engine: Engine, action: str, reason: str
) -> None:
    p = post(scenario)
    with operator_engine.connect() as c, pytest.raises(DBAPIError) as error:
        c.execute(
            text("SELECT payment.operator_action(:id,:action,:reason,:corr)"),
            {"id": UUID(str(p["id"])), "action": action, "reason": reason, "corr": uuid4()},
        )
    assert getattr(error.value.orig, "sqlstate", None) == "22023"
    assert counts(scenario) == (1, 1, 1)


def test_crash_exhaustion_audit_keeps_payment_correlation(
    scenario: Scenario, worker_engine: Engine
) -> None:
    p = post(scenario)
    for _ in range(6):
        make_due(scenario.database, expire=True)
        assert worker.claim(worker_engine) is not None
    with scenario.database.connect(user="payment_migrator") as c:
        assert c.execute(
            "SELECT resource_id,correlation_id FROM payment.audit_events "
            "WHERE event_type='payment_needs_review'"
        ).fetchall() == [(UUID(str(p["id"])), UUID(str(p["id"])))]
    assert amounts(scenario) == (99000, 1000, 0)


def test_cli_operator_commands_use_restricted_login(scenario: Scenario, tmp_path: Path) -> None:
    import json
    import os
    import subprocess
    import sys

    p = post(scenario)
    secret = SecretStr(uuid4().hex)
    with scenario.database.connect(user="payment_migrator") as c:
        provision_operator(c, secret)
    with live_provider(tmp_path / "provider.db") as provider:
        environment = {
            **os.environ,
            "OPERATOR_DB_HOST": scenario.database.settings.db_host,
            "OPERATOR_DB_PORT": str(scenario.database.settings.db_port),
            "OPERATOR_DB_NAME": scenario.database.settings.db_name,
            "OPERATOR_DB_PASSWORD": secret.get_secret_value(),
            "OPERATOR_PROVIDER_URL": provider.base_url,
        }
        for arguments, expected in [
            (["metrics"], 0),
            (["reconcile"], 1),
            (["lookup", str(p["id"]), "--reason", "INC-1"], 0),
            (["retry", str(p["id"]), "--reason", "INC-1"], 0),
            (["retry", str(p["id"]), "--reason", "invalid!"], 2),
        ]:
            completed = subprocess.run(
                [sys.executable, "-m", "payment_ledger.operations.cli", *arguments],
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert completed.returncode == expected
            assert secret.get_secret_value() not in completed.stdout + completed.stderr
            report = json.loads(completed.stdout)
            UUID(report["correlation_id"])
    assert counts(scenario) == (1, 1, 1)
    with scenario.database.connect(user="payment_migrator") as c:
        assert c.execute("SELECT DISTINCT actor FROM payment.operator_events").fetchall() == [
            ("payment_operator_login",)
        ]


@pytest.mark.migration_revision("0005_payments")
def test_readiness_requires_m6_counter_privilege(database: Database) -> None:
    from fastapi.testclient import TestClient

    from payment_ledger.main import create_app

    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
        with database.connect(user="payment_migrator") as c:
            c.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "payment.take_financial_slot(uuid,text,text,integer,integer) FROM payment_app"
            )
        assert client.get("/health/ready").status_code == 503
