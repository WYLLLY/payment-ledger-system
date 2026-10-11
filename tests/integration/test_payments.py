"""M5 real PostgreSQL + real HTTP/SQLite, deterministic lock and failure evidence."""

import asyncio
import socket
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event, Thread
from time import monotonic
from typing import Literal
from uuid import UUID, uuid4

import psycopg
import pytest
import uvicorn
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, InsufficientPrivilege, UniqueViolation
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from payment_ledger.database import create_database_engine
from payment_ledger.main import create_app
from payment_ledger.payments import service, worker
from payment_ledger.payments.provider import ProviderClient, ProviderRequest
from payment_ledger.payments.schemas import PaymentRequest
from payment_ledger.payments.simulator import Mode, ProviderStore, create_simulator
from payment_ledger.transfers.service import StoredResponse
from scripts.seed_demo import provision_dev_role

from .conftest import Database
from .test_ledger import assert_clean, opening, wallet
from .test_transfers import Scenario, authenticate

pytestmark = pytest.mark.integration


@pytest.fixture(name="scenario")
def payment_scenario(database: Database) -> Iterator[Scenario]:
    alice, source = wallet(database)
    bob, destination = wallet(database)
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    opening(database, alice, source)
    with TestClient(create_app(database.settings)) as client:
        yield Scenario(
            database,
            client,
            alice,
            bob,
            source,
            destination,
            authenticate(database, alice),
            authenticate(database, bob),
        )


def payload(s: Scenario, amount: int = 1000) -> dict[str, object]:
    return {
        "wallet_id": str(s.source),
        "recipient": "merchant-1",
        "currency": "EUR",
        "amount_minor": amount,
    }


def post(
    s: Scenario, key: str = "pay-1", body: dict[str, object] | None = None
) -> dict[str, object]:
    response = s.client.post(
        "/payments",
        json=payload(s) if body is None else body,
        headers={**s.alice_headers, "Idempotency-Key": key},
    )
    assert response.status_code == 202, response.text
    result: dict[str, object] = response.json()
    return result


def payment_state(s: Scenario, payment_id: object) -> str:
    return str(s.client.get(f"/payments/{payment_id}", headers=s.alice_headers).json()["state"])


def amounts(s: Scenario) -> tuple[int, int, int]:
    with s.database.connect() as c:
        row = c.execute(
            """
            SELECT (SELECT balance_minor FROM payment.ledger_accounts
                    WHERE wallet_id=%s AND account_kind='available'),
                   (SELECT balance_minor FROM payment.ledger_accounts
                    WHERE wallet_id=%s AND account_kind='reserved'),
                   (SELECT balance_minor FROM payment.ledger_accounts
                    WHERE wallet_id IS NULL AND account_kind='clearing' AND currency='EUR')
        """,
            (s.source, s.source),
        ).fetchone()
    assert row is not None
    return int(str(row[0])), int(str(row[1])), int(str(row[2]))


def counts(s: Scenario) -> tuple[int, int, int]:
    with s.database.connect(user="payment_migrator") as c:
        row = c.execute("""SELECT (SELECT count(*) FROM payment.payments),
            (SELECT count(*) FROM payment.payment_outbox),
            (SELECT count(*) FROM payment.ledger_journals
                WHERE payment_id IS NOT NULL)""").fetchone()
    assert row is not None
    return int(str(row[0])), int(str(row[1])), int(str(row[2]))


@pytest.fixture
def worker_engine(database: Database) -> Iterator[Engine]:
    def connect() -> psycopg.Connection[tuple[object, ...]]:
        connection = database.connect(user="postgres")
        connection.execute("SET SESSION AUTHORIZATION payment_worker")
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


@contextmanager
def live_provider(path: Path, mode: Mode = "success") -> Iterator[ProviderClient]:
    app = create_simulator(path, mode=mode, delay=0.2)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = monotonic() + 5
    while not server.started:
        assert thread.is_alive() and monotonic() < deadline, "provider startup failed"
        Event().wait(0.005)
    try:
        yield ProviderClient(f"http://127.0.0.1:{port}", timeout=0.08 if "timeout" in mode else 2)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def make_due(database: Database, *, expire: bool = False) -> None:
    with database.connect(user="payment_migrator") as c:
        c.execute(
            "UPDATE payment.payment_outbox SET "
            "next_attempt_at=clock_timestamp()-interval '1 second'"
        )
        if expire:
            c.execute(
                "UPDATE payment.payment_outbox SET "
                "lease_until=clock_timestamp()-interval '1 second'"
            )


def provider_request(body: dict[str, object]) -> ProviderRequest:
    return ProviderRequest.model_validate(
        {
            "payment_id": body["id"],
            **{key: body[key] for key in ("recipient", "amount_minor", "currency")},
        }
    )


def wait_for_blockers(database: Database, sql_fragment: str, count: int = 1) -> None:
    deadline = monotonic() + 5
    with database.connect(user="postgres", autocommit=True) as observer:
        while monotonic() < deadline:
            row = observer.execute(
                """SELECT count(*) FROM pg_stat_activity
                WHERE datname=current_database() AND wait_event_type='Lock'
                    AND query LIKE %s""",
                (f"%{sql_fragment}%",),
            ).fetchone()
            if row == (count,):
                return
    raise AssertionError("expected lock contention was not observed")


def test_creation_replay_conflict_authorization_and_atomic_reservation(scenario: Scenario) -> None:
    s = scenario
    original = post(s)
    assert original["state"] == "pending"
    assert amounts(s) == (99000, 1000, 0)
    assert post(s) == original
    assert counts(s) == (1, 1, 1)
    for field, value in (
        ("amount_minor", 2000),
        ("recipient", "merchant-2"),
        ("currency", "USD"),
        ("wallet_id", str(s.destination)),
    ):
        changed = {**payload(s), field: value}
        response = s.client.post(
            "/payments", json=changed, headers={**s.alice_headers, "Idempotency-Key": "pay-1"}
        )
        assert response.status_code == 409
    assert s.client.get(f"/payments/{original['id']}", headers=s.bob_headers).status_code == 404
    assert s.client.get("/payments", headers=s.bob_headers).json() == []
    assert s.client.get("/payments", headers=s.alice_headers).json()[0]["id"] == original["id"]
    assert s.client.get("/payments?offset=1", headers=s.alice_headers).json() == []
    assert (
        s.client.post("/payments", json=payload(s), headers={"Idempotency-Key": "x"}).status_code
        == 401
    )
    assert (
        s.client.post(
            "/payments", json=payload(s), headers={**s.bob_headers, "Idempotency-Key": "x"}
        ).status_code
        == 404
    )
    # The same textual key is independently scoped to transfers.
    assert s.post("pay-1")[0] == 201
    assert_clean(s.database)


@pytest.mark.parametrize("amount", [0, -1, 1000000000001, 1.5, 1.0, True, "1", None])
def test_invalid_payment_amount_does_not_claim_key(scenario: Scenario, amount: object) -> None:
    s = scenario
    response = s.client.post(
        "/payments",
        json={**payload(s), "amount_minor": amount},
        headers={**s.alice_headers, "Idempotency-Key": "x"},
    )
    assert response.status_code == 422
    assert counts(s) == (0, 0, 0)


@pytest.mark.parametrize("key", [None, "", "has space", "x" * 129])
def test_payment_key_required(scenario: Scenario, key: str | None) -> None:
    headers = dict(scenario.alice_headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    assert (
        scenario.client.post("/payments", json=payload(scenario), headers=headers).status_code
        == 422
    )


@pytest.mark.parametrize("mode,expected", [("success", "succeeded"), ("decline", "failed")])
def test_worker_terminal_http_and_replay(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path, mode: Mode, expected: str
) -> None:
    s = scenario
    first = post(s)
    with live_provider(tmp_path / "provider.db", mode) as provider:
        assert asyncio.run(worker.run_once(worker_engine, provider))
        assert not asyncio.run(worker.run_once(worker_engine, provider))
        assert provider.lookup(provider_request(first)) == expected
    assert payment_state(s, first["id"]) == expected
    assert amounts(s) == ((99000, 0, 1000) if expected == "succeeded" else (100000, 0, 0))
    assert post(s) == first  # Initial 202 remains stable even after terminal transition.
    assert counts(s) == (1, 1, 2)
    with s.database.connect(user="payment_migrator") as c:
        assert c.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type=%s",
            (f"payment_{expected}",),
        ).fetchone() == (1,)
    history = s.client.get(f"/wallets/{s.source}/ledger", headers=s.alice_headers)
    assert history.status_code == 200
    assert_clean(s.database)


@pytest.mark.parametrize("mode", ["timeout_before", "timeout_after", "unavailable"])
def test_unknown_outcomes_keep_reserve_and_reconcile(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path, mode: Mode
) -> None:
    s = scenario
    first = post(s)
    path = tmp_path / "provider.db"
    with live_provider(path, mode) as provider:
        assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, first["id"]) == "pending"
    assert amounts(s) == (99000, 1000, 0)
    assert counts(s) == (1, 1, 1)
    make_due(s.database)
    with live_provider(path) as provider:
        assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, first["id"]) == "succeeded"
    assert amounts(s) == (99000, 0, 1000)
    assert_clean(s.database)


def test_retry_exhaustion_queries_only_and_eventual_reconciliation(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path
) -> None:
    s = scenario
    first = post(s)
    path = tmp_path / "provider.db"
    with live_provider(path, "unavailable") as provider:
        for _ in range(5):
            make_due(s.database)
            assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, first["id"]) == "needs_review"
    assert amounts(s) == (99000, 1000, 0)
    # Recovery sees absence: it cannot resubmit or release exhausted work.
    with live_provider(path) as provider:
        make_due(s.database)
        assert asyncio.run(worker.run_once(worker_engine, provider))
        assert provider.lookup(provider_request(first)) == "not_found"
        assert payment_state(s, first["id"]) == "needs_review"
        # A late definitive outcome becomes visible (simulates remote delayed completion).
        assert provider.submit(provider_request(first)) == "succeeded"
        make_due(s.database)
        assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, first["id"]) == "succeeded"
    with s.database.connect(user="payment_migrator") as c:
        assert c.execute("SELECT attempts FROM payment.payment_outbox").fetchone() == (5,)
        assert c.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='payment_needs_review'"
        ).fetchone() == (1,)
    assert_clean(s.database)


@pytest.mark.parametrize("phase", ["before_http", "after_remote", "before_local_commit"])
def test_crash_recovery_persistent_provider(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path, phase: str
) -> None:
    s = scenario
    first = post(s)
    item = worker.claim(worker_engine)
    assert item is not None
    path = tmp_path / "provider.db"
    if phase != "before_http":
        with live_provider(path) as provider:
            assert provider.submit(item.payment) == "succeeded"
    if phase == "before_local_commit":
        # Execute full finalization, then terminate its actual PostgreSQL backend before commit.
        with worker_engine.connect() as connection:
            tx = connection.begin()
            pid = connection.scalar(text("SELECT pg_backend_pid()"))
            assert connection.scalar(
                text("SELECT payment.finish_payment(:id,:generation,'succeeded')"),
                {"id": first["id"], "generation": item.generation},
            )
            with s.database.connect(user="postgres") as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
            with pytest.raises(DBAPIError):
                tx.commit()
    assert amounts(s) == (99000, 1000, 0)
    make_due(s.database, expire=True)
    with live_provider(path) as provider:
        assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, first["id"]) == "succeeded"
    assert counts(s) == (1, 1, 2)
    assert_clean(s.database)


def test_stale_worker_cannot_overwrite_new_generation(
    scenario: Scenario, worker_engine: Engine
) -> None:
    s = scenario
    first = post(s)
    old = worker.claim(worker_engine)
    assert old is not None
    assert worker.claim(worker_engine) is None
    make_due(s.database, expire=True)
    assert not worker.finish(worker_engine, UUID(str(first["id"])), old.generation, "failed")
    new = worker.claim(worker_engine)
    assert new is not None and new.generation == old.generation + 1
    for outcome in ("succeeded", "failed", "unknown"):
        assert not worker.finish(worker_engine, new.payment.payment_id, old.generation, outcome)
    assert worker.finish(worker_engine, new.payment.payment_id, new.generation, "succeeded")
    assert not worker.finish(worker_engine, new.payment.payment_id, new.generation, "failed")
    assert amounts(s) == (99000, 0, 1000)
    assert_clean(s.database)


def test_repeated_crashes_exhaust_claim_budget(scenario: Scenario, worker_engine: Engine) -> None:
    first = post(scenario)
    for attempt in range(1, 6):
        item = worker.claim(worker_engine)
        assert item is not None and item.attempts == attempt
        make_due(scenario.database, expire=True)
    item = worker.claim(worker_engine)
    assert item is not None and item.state == "needs_review" and item.attempts == 5
    assert payment_state(scenario, first["id"]) == "needs_review"
    assert amounts(scenario) == (99000, 1000, 0)


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_creation_observed_wait_prevents_double_spend(
    scenario: Scenario, same_key: bool
) -> None:
    s = scenario
    engine = create_database_engine(s.database.settings)
    barrier = Barrier(3)
    request = PaymentRequest.model_validate(payload(s, 60000))

    def submit(index: int) -> StoredResponse:
        barrier.wait(timeout=5)
        return service.create_payment(
            engine, s.alice, "same" if same_key else f"key-{index}", request
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            with s.database.connect(user="payment_migrator") as blocker:
                blocker.execute(
                    "SELECT id FROM payment.ledger_accounts WHERE wallet_id=%s ORDER "
                    "BY id FOR UPDATE",
                    (s.source,),
                )
                jobs = [executor.submit(submit, i) for i in range(2)]
                barrier.wait(timeout=5)
                wait_for_blockers(s.database, "payment.create_payment", 2)
                blocker.commit()
            results = [job.result(timeout=5) for job in jobs]
        assert sorted(r.status_code for r in results) == ([202, 202] if same_key else [202, 409])
        if same_key:
            assert results[0] == results[1]
        assert amounts(s) == (40000, 60000, 0)
        assert counts(s) == (1, 1, 1)
        assert_clean(s.database)
    finally:
        engine.dispose()


def test_two_workers_skip_locked_and_terminal_race(
    scenario: Scenario, worker_engine: Engine
) -> None:
    s = scenario
    one, two = post(s, "one"), post(s, "two")
    with worker_engine.begin() as holder:
        held = holder.execute(text("SELECT * FROM payment.claim_payment(30)")).mappings().one()
        other = worker.claim(worker_engine)
        assert other is not None and str(other.payment.payment_id) != str(held["payment_id"])
    barrier = Barrier(3)

    def finalize(outcome: Literal["succeeded", "failed"]) -> bool:
        barrier.wait(timeout=5)
        return worker.finish(worker_engine, other.payment.payment_id, other.generation, outcome)

    with ThreadPoolExecutor(max_workers=2) as executor:
        with s.database.connect(user="payment_migrator") as blocker:
            blocker.execute(
                "SELECT payment_id FROM payment.payment_outbox WHERE payment_id=%s FOR UPDATE",
                (other.payment.payment_id,),
            )
            outcomes: tuple[Literal["succeeded", "failed"], ...] = ("succeeded", "failed")
            jobs = [executor.submit(finalize, outcome) for outcome in outcomes]
            barrier.wait(timeout=5)
            wait_for_blockers(s.database, "payment.finish_payment", 2)
            blocker.commit()
        assert sorted(job.result(timeout=5) for job in jobs) == [False, True]
    assert worker.finish(
        worker_engine, UUID(str(held["payment_id"])), int(held["generation"]), "succeeded"
    )
    assert counts(s) == (2, 2, 4)
    assert {payment_state(s, body["id"]) for body in (one, two)} <= {"succeeded", "failed"}
    assert_clean(s.database)


@pytest.mark.parametrize("table", ["payment_outbox", "audit_events", "idempotency_records"])
def test_creation_rolls_back_every_effect(scenario: Scenario, table: str) -> None:
    s = scenario
    action = "UPDATE" if table == "idempotency_records" else "INSERT"
    with s.database.connect(user="payment_migrator") as c:
        c.execute(
            "CREATE FUNCTION payment.m5_fault() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'injected fault'; END $$"
        )
        c.execute(
            f"CREATE TRIGGER m5_fault BEFORE {action} ON payment.{table} "
            "FOR EACH ROW EXECUTE FUNCTION payment.m5_fault()"
        )
    response = s.client.post(
        "/payments", json=payload(s), headers={**s.alice_headers, "Idempotency-Key": "x"}
    )
    assert response.status_code == 503
    assert counts(s) == (0, 0, 0)
    assert amounts(s) == (100000, 0, 0)
    assert_clean(s.database)


@pytest.mark.parametrize("role", ["payment_app", "payment_worker"])
@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE payment.payments SET state='failed'",
        "DELETE FROM payment.payments",
        "TRUNCATE payment.payments CASCADE",
        "UPDATE payment.payment_outbox SET generation=99",
        "UPDATE payment.ledger_accounts SET balance_minor=0",
        "DELETE FROM payment.ledger_entries",
        "INSERT INTO payment.ledger_journals(kind,currency) VALUES('settlement','EUR')",
    ],
)
def test_runtime_roles_cannot_bypass_payment_protocol(
    scenario: Scenario, role: str, statement: str
) -> None:
    post(scenario)
    with pytest.raises(InsufficientPrivilege), scenario.database.connect(user="postgres") as c:
        c.execute(f"SET SESSION AUTHORIZATION {role}")
        c.execute(statement)
    assert counts(scenario) == (1, 1, 1)


def test_role_function_boundaries_and_terminal_uniqueness(
    scenario: Scenario, worker_engine: Engine
) -> None:
    s = scenario
    body = post(s)
    with pytest.raises(InsufficientPrivilege), s.database.connect() as c:
        c.execute("SELECT * FROM payment.claim_payment(30)")
    with pytest.raises(DBAPIError), worker_engine.begin() as c:
        c.execute(
            text("SELECT * FROM payment.create_payment(:owner,'x',:wallet,'test',1,'EUR')"),
            {"owner": s.alice, "wallet": s.source},
        )
    item = worker.claim(worker_engine)
    assert item is not None
    assert worker.finish(worker_engine, item.payment.payment_id, item.generation, "succeeded")
    with pytest.raises(UniqueViolation), s.database.connect(user="payment_migrator") as c:
        c.execute(
            "INSERT INTO payment.ledger_journals(kind,payment_id,currency) "
            "VALUES('release',%s,'EUR')",
            (body["id"],),
        )
    with pytest.raises(CheckViolation), s.database.connect(user="payment_migrator") as c:
        c.execute("UPDATE payment.payments SET state='failed' WHERE id=%s", (body["id"],))
    with pytest.raises(CheckViolation), s.database.connect(user="payment_migrator") as c:
        c.execute("UPDATE payment.payments SET amount_minor=2 WHERE id=%s", (body["id"],))
    assert_clean(s.database)


def test_provider_persistence_conflicts_and_concurrent_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "provider.db"
    request = ProviderRequest(
        payment_id=uuid4(), recipient="vendor", amount_minor=42, currency="JPY"
    )
    with live_provider(path) as provider:
        barrier = Barrier(3)

        def submit() -> str:
            barrier.wait(timeout=5)
            return provider.submit(request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(submit) for _ in range(2)]
            barrier.wait(timeout=5)
            assert [job.result(timeout=5) for job in jobs] == ["succeeded", "succeeded"]
        changed = request.model_copy(update={"amount_minor": 43})
        assert provider.submit(changed) == "unknown"
    # Reopen server/storage from disk: no in-memory idempotency state survives.
    with live_provider(path, "decline") as provider:
        assert provider.lookup(request) == "succeeded"
        assert provider.submit(request) == "succeeded"
    with TestClient(create_simulator(path)) as client:
        assert (
            client.post(
                "/payments",
                json=request.model_dump(mode="json"),
                headers={"Idempotency-Key": "contradiction"},
            ).status_code
            == 409
        )
    store = ProviderStore(path)
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM outcomes").fetchone() == (1,)


@pytest.mark.migration_revision("0004_transfers")
def test_m4_upgrade_retains_transfers_and_requires_m5(database: Database) -> None:
    owner, source = wallet(database)
    _, target = wallet(database)
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    opening(database, owner, source)
    with database.connect() as connection:
        result = connection.execute(
            "SELECT * FROM payment.create_transfer(%s,'pre-m5',%s,%s,1000,'EUR')",
            (owner, source, target),
        ).fetchone()
        assert result is not None and result[0] == 201
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
        with database.connect(user="payment_migrator") as c:
            c.execute(
                "REVOKE EXECUTE ON FUNCTION "
                "payment.create_payment(uuid,text,uuid,text,bigint,text) FROM "
                "payment_app"
            )
        assert client.get("/health/ready").status_code == 503
        database.migrate("downgrade", "0004_transfers")
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.transfers").fetchone() == (1,)
    assert_clean(database)


def test_populated_payment_downgrade_refused(scenario: Scenario) -> None:
    post(scenario)
    with pytest.raises(RuntimeError, match="cannot downgrade retained payment history"):
        scenario.database.migrate("downgrade", "0004_transfers")
    assert counts(scenario) == (1, 1, 1)
    assert_clean(scenario.database)


def test_worker_real_login_has_only_worker_capabilities(scenario: Scenario) -> None:
    from scripts.provision_app import Credentials, provision_worker

    s = scenario
    credentials = Credentials()
    with s.database.connect(user="payment_migrator") as c:
        provision_worker(c, credentials.worker_db_password)
    settings = worker.WorkerSettings(
        db_host=s.database.settings.db_host,
        db_port=s.database.settings.db_port,
        db_name=s.database.settings.db_name,
        db_password=credentials.worker_db_password,
    )
    engine = worker.create_worker_engine(settings)
    try:
        post(s)
        item = worker.claim(engine)
        assert item is not None
        assert worker.finish(engine, item.payment.payment_id, item.generation, "succeeded")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "payment_worker_login"
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text("UPDATE payment.ledger_accounts SET balance_minor=0"))
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text("SELECT * FROM payment.sessions"))
        with s.database.connect(user="postgres") as c:
            assert c.execute(
                "SELECT rolcanlogin FROM pg_roles WHERE rolname='payment_worker'"
            ).fetchone() == (False,)
    finally:
        engine.dispose()
    assert_clean(s.database)


def test_postgres_retry_aborts_entire_creation(scenario: Scenario) -> None:
    s = scenario
    with s.database.connect(user="payment_migrator") as c:
        c.execute("""CREATE SEQUENCE payment.retry_count;
            CREATE FUNCTION payment.retry_test() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF nextval('payment.retry_count')<3 THEN
                    RAISE EXCEPTION 'retry' USING ERRCODE='40001';
                END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER retry_test BEFORE INSERT ON payment.audit_events
                FOR EACH ROW EXECUTE FUNCTION payment.retry_test();""")
    post(s)
    assert counts(s) == (1, 1, 1)
    assert amounts(s) == (99000, 1000, 0)
    with s.database.connect(user="payment_migrator") as c:
        assert c.execute("SELECT last_value FROM payment.retry_count").fetchone() == (3,)
    assert_clean(s.database)


def test_transfer_and_payment_contend_without_double_spend(scenario: Scenario) -> None:
    from payment_ledger.transfers.schemas import TransferRequest
    from payment_ledger.transfers.service import create_transfer

    s = scenario
    engine = create_database_engine(s.database.settings)
    barrier = Barrier(3)

    def pay() -> StoredResponse:
        barrier.wait(timeout=5)
        return service.create_payment(
            engine, s.alice, "payment", PaymentRequest.model_validate(payload(s, 60000))
        )

    def transfer() -> StoredResponse:
        barrier.wait(timeout=5)
        return create_transfer(
            engine, s.alice, "transfer", TransferRequest.model_validate(s.payload(60000))
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            with s.database.connect(user="payment_migrator") as blocker:
                blocker.execute("SELECT id FROM payment.ledger_accounts ORDER BY id FOR UPDATE")
                jobs = [executor.submit(pay), executor.submit(transfer)]
                barrier.wait(timeout=5)
                wait_for_blockers(s.database, "payment.create_", 2)
                blocker.commit()
            results = [job.result(timeout=5).status_code for job in jobs]
        assert 409 in results and len({201, 202}.intersection(results)) == 1
        assert amounts(s)[0] == 40000
        assert_clean(s.database)
    finally:
        engine.dispose()


def test_no_database_transaction_during_provider_io(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path
) -> None:
    from sqlalchemy import event

    from payment_ledger.payments.provider import Outcome

    active: list[object] = []

    def checkout(dbapi: object, record: object, proxy: object) -> None:
        active.append(record)

    def checkin(dbapi: object, record: object) -> None:
        active.remove(record)

    event.listen(worker_engine, "checkout", checkout)
    event.listen(worker_engine, "checkin", checkin)

    class ObservedProvider(ProviderClient):
        def lookup(self, payment: ProviderRequest) -> Outcome:
            assert not active
            return super().lookup(payment)

        def submit(self, payment: ProviderRequest) -> Outcome:
            assert not active
            return super().submit(payment)

    post(scenario)
    with live_provider(tmp_path / "provider.db") as provider:
        assert asyncio.run(worker.run_once(worker_engine, ObservedProvider(provider.base_url)))
    assert not active


def test_worker_process_killed_after_provider_persists_then_recovers(
    scenario: Scenario, worker_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os
    import subprocess
    import sys

    from scripts.provision_app import Credentials, provision_worker

    s = scenario
    body = post(s)
    accepted = Event()
    release_response = Event()
    original = ProviderStore.submit

    def observed(
        store: ProviderStore, key: str, payment: ProviderRequest, *, decline: bool
    ) -> dict[str, object]:
        result = original(store, key, payment, decline=decline)
        accepted.set()  # SQLite commit is durable; the response waits for the kill.
        assert release_response.wait(timeout=10)
        return result

    monkeypatch.setattr(ProviderStore, "submit", observed)
    credentials = Credentials()
    with s.database.connect(user="payment_migrator") as c:
        provision_worker(c, credentials.worker_db_password)
    path = tmp_path / "provider.db"
    with live_provider(path, "timeout_after") as provider:
        environment = {
            **os.environ,
            "WORKER_DB_HOST": s.database.settings.db_host,
            "WORKER_DB_PORT": str(s.database.settings.db_port),
            "WORKER_DB_NAME": s.database.settings.db_name,
            "WORKER_DB_PASSWORD": credentials.worker_db_password.get_secret_value(),
            "WORKER_PROVIDER_URL": provider.base_url,
            "WORKER_HTTP_TIMEOUT": "3",
        }
        process = subprocess.Popen(
            [sys.executable, "-m", "payment_ledger.payments.worker"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert accepted.wait(timeout=10), "worker did not reach provider"
            process.kill()
            process.wait(timeout=5)
        finally:
            release_response.set()
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert payment_state(s, body["id"]) == "pending"
    assert amounts(s) == (99000, 1000, 0)
    make_due(s.database, expire=True)
    with live_provider(path) as provider:
        assert asyncio.run(worker.run_once(worker_engine, provider))
    assert payment_state(s, body["id"]) == "succeeded"
    assert counts(s) == (1, 1, 2)
    assert_clean(s.database)
