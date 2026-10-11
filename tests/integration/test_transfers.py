"""M4 API, real PostgreSQL transactions/ACLs and deterministic concurrency."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Barrier
from time import monotonic
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import CheckViolation, InsufficientPrivilege

from payment_ledger.database import create_database_engine
from payment_ledger.main import create_app
from payment_ledger.security import new_session_token, token_digest
from payment_ledger.transfers import service
from payment_ledger.transfers.schemas import TransferRequest
from scripts.seed_demo import provision_dev_role

from .conftest import Database
from .test_ledger import accounts, assert_clean, opening, wallet

pytestmark = pytest.mark.integration


@dataclass
class Scenario:
    database: Database
    client: TestClient
    alice: UUID
    bob: UUID
    source: UUID
    destination: UUID
    alice_headers: dict[str, str]
    bob_headers: dict[str, str]

    def payload(self, amount: int = 1000) -> dict[str, object]:
        return {
            "source_wallet_id": str(self.source),
            "destination_wallet_id": str(self.destination),
            "currency": "EUR",
            "amount_minor": amount,
        }

    def post(self, key: str, payload: dict[str, object] | None = None) -> tuple[int, object]:
        response = self.client.post(
            "/transfers",
            json=self.payload() if payload is None else payload,
            headers={**self.alice_headers, "Idempotency-Key": key},
        )
        return response.status_code, response.json()


def authenticate(database: Database, owner: UUID) -> dict[str, str]:
    token = new_session_token()
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO payment.sessions(token_hash,user_id,expires_at) "
            "VALUES (%s,%s,statement_timestamp()+interval '1 hour')",
            (token_digest(token), owner),
        )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def scenario(database: Database) -> Iterator[Scenario]:
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


def counts(database: Database) -> tuple[int, int, int, int]:
    with database.connect(user="payment_migrator") as connection:
        row = connection.execute("""
            SELECT (SELECT count(*) FROM payment.transfers),
                   (SELECT count(*) FROM payment.ledger_journals WHERE kind='transfer'),
                   (SELECT count(*) FROM payment.audit_events WHERE event_type='transfer_created'),
                   (SELECT count(*) FROM payment.idempotency_records)
        """).fetchone()
        assert row is not None
        a, b, c, d = (int(str(value)) for value in row)
        return a, b, c, d


def balances(s: Scenario) -> tuple[int, int]:
    with s.database.connect() as connection:
        row = connection.execute(
            "SELECT (SELECT balance_minor FROM payment.ledger_accounts "
            "WHERE wallet_id=%s AND account_kind='available'), "
            "(SELECT balance_minor FROM payment.ledger_accounts "
            "WHERE wallet_id=%s AND account_kind='available')",
            (s.source, s.destination),
        ).fetchone()
        assert row is not None
        return int(str(row[0])), int(str(row[1]))


def test_transfer_replay_stores_original_response_and_history(scenario: Scenario) -> None:
    s = scenario
    first = s.post("request-1")
    assert first[0] == 201
    equivalent = s.payload()
    equivalent["source_wallet_id"] = s.source.hex.upper()
    assert s.post("request-1", equivalent) == first
    assert counts(s.database) == (1, 1, 1, 1)
    assert balances(s) == (99000, 1000)
    assert isinstance(first[1], dict)
    path = f"/transfers/{first[1]['id']}"
    response = s.client.get(path, headers=s.alice_headers)
    assert response.status_code == 200
    assert response.json()["id"] == first[1]["id"]
    assert s.client.get("/transfers", headers=s.alice_headers).json() == [response.json()]
    assert s.client.get("/transfers?offset=1", headers=s.alice_headers).json() == []
    for target, headers, amount in (
        (s.source, s.alice_headers, -1000),
        (s.destination, s.bob_headers, 1000),
    ):
        entries = s.client.get(f"/wallets/{target}/ledger", headers=headers).json()
        assert [e["amount_minor"] for e in entries if e["kind"] == "transfer"] == [amount]
    assert_clean(s.database)


@pytest.mark.parametrize(
    "field", ["amount_minor", "currency", "source_wallet_id", "destination_wallet_id"]
)
def test_reused_key_with_changed_payload_conflicts(scenario: Scenario, field: str) -> None:
    s = scenario
    assert s.post("same")[0] == 201
    payload = s.payload()
    payload[field] = {
        "amount_minor": 2000,
        "currency": "USD",
        "source_wallet_id": str(uuid4()),
        "destination_wallet_id": str(uuid4()),
    }[field]
    assert s.post("same", payload) == (409, {"detail": "Idempotency key conflict"})
    assert counts(s.database) == (1, 1, 1, 1)
    assert balances(s) == (99000, 1000)


@pytest.mark.parametrize("amount", [0, -1, 1000000000001, 1.5, 1.0, True, "1", None])
def test_invalid_amount_never_claims_key(scenario: Scenario, amount: object) -> None:
    payload = scenario.payload()
    payload["amount_minor"] = amount
    assert scenario.post("invalid", payload)[0] == 422
    assert counts(scenario.database) == (0, 0, 0, 0)


@pytest.mark.parametrize("key", [None, "", "has space", "a" * 129])
def test_key_is_required_and_bounded(scenario: Scenario, key: str | None) -> None:
    headers = dict(scenario.alice_headers)
    if key is not None:
        headers["Idempotency-Key"] = key
    response = scenario.client.post("/transfers", headers=headers, json=scenario.payload())
    assert response.status_code == 422
    assert counts(scenario.database) == (0, 0, 0, 0)


def test_resources_private_and_authentication_required(scenario: Scenario) -> None:
    s = scenario
    assert (
        s.client.post(
            "/transfers", json=s.payload(), headers={"Idempotency-Key": "auth"}
        ).status_code
        == 401
    )
    status, body = s.post("own")
    assert status == 201 and isinstance(body, dict)
    for headers in (s.bob_headers, authenticate(s.database, wallet(s.database)[0])):
        denied = s.client.get(f"/transfers/{body['id']}", headers=headers)
        assert denied.status_code == 404
        assert denied.json() == s.client.get(f"/transfers/{uuid4()}", headers=headers).json()
        assert s.client.get("/transfers", headers=headers).json() == []
    payload = s.payload()
    payload["source_wallet_id"] = str(s.destination)
    payload["destination_wallet_id"] = str(s.source)
    assert s.post("foreign", payload) == (404, {"detail": "Resource not found"})
    payload["source_wallet_id"] = str(uuid4())
    assert s.post("absent", payload) == (404, {"detail": "Resource not found"})
    assert balances(s) == (99000, 1000)


def test_rejected_result_is_retained_after_balance_changes(scenario: Scenario) -> None:
    s = scenario
    rejected = s.post("insufficient", s.payload(100001))
    assert rejected == (409, {"detail": "Insufficient funds"})
    opening(s.database, s.bob, s.destination)
    result = s.client.post(
        "/transfers",
        headers={**s.bob_headers, "Idempotency-Key": "top-up"},
        json={
            "source_wallet_id": str(s.destination),
            "destination_wallet_id": str(s.source),
            "amount_minor": 1000,
            "currency": "EUR",
        },
    )
    assert result.status_code == 201
    assert s.post("insufficient", s.payload(100001)) == rejected
    assert s.post("insufficient", s.payload(1))[0] == 409
    assert s.post("new-key", s.payload(100001))[0] == 201
    assert_clean(s.database)


def test_same_key_is_scoped_per_user(scenario: Scenario) -> None:
    s = scenario
    assert s.post("shared")[0] == 201
    response = s.client.post(
        "/transfers",
        headers={**s.bob_headers, "Idempotency-Key": "shared"},
        json={
            "source_wallet_id": str(s.destination),
            "destination_wallet_id": str(s.source),
            "amount_minor": 1000,
            "currency": "EUR",
        },
    )
    assert response.status_code == 201
    assert counts(s.database) == (2, 2, 2, 2)
    assert balances(s) == (100000, 0)


@pytest.mark.parametrize("case", ["same_wallet", "currency", "missing_destination"])
def test_invalid_business_request_is_retained(scenario: Scenario, case: str) -> None:
    s = scenario
    payload = s.payload()
    if case == "same_wallet":
        payload["destination_wallet_id"] = str(s.source)
    elif case == "currency":
        payload["currency"] = "JPY"
    else:
        payload["destination_wallet_id"] = str(uuid4())
    result = s.post(case, payload)
    assert result[0] == (404 if case == "missing_destination" else 422)
    assert s.post(case, payload) == result
    assert counts(s.database) == (0, 0, 0, 1)
    assert balances(s) == (100000, 0)


@pytest.mark.parametrize("role", ["payment_app", "payment_worker", "payment_dev"])
def test_financial_tables_and_private_helpers_are_not_writable(
    scenario: Scenario, role: str
) -> None:
    statements = [
        f"{prefix} payment.{table}{suffix}"
        for table in ("transfers", "idempotency_records")
        for prefix, suffix in [
            ("INSERT INTO", " DEFAULT VALUES"),
            ("UPDATE", " SET user_id=gen_random_uuid()"),
            ("DELETE FROM", ""),
            ("TRUNCATE", " CASCADE"),
        ]
    ] + [
        "SELECT payment.post_pair(NULL,NULL,NULL,1::bigint,'EUR')",
        "SELECT payment.check_transfer_complete()",
        "SELECT payment.check_idempotency_complete()",
    ]
    if role != "payment_app":
        statements.append(
            "SELECT * FROM payment.create_transfer(NULL,'key',NULL,NULL,1::bigint,'EUR')"
        )
    for statement in statements:
        with scenario.database.connect(user="postgres") as connection:
            connection.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))
            with pytest.raises(InsufficientPrivilege):
                connection.execute(statement)


def test_direct_runtime_call_checks_owner_and_stores_request_hash(scenario: Scenario) -> None:
    s = scenario
    with s.database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM payment.create_transfer(%s,'direct',%s,%s,1::bigint,'EUR')",
            (s.bob, s.source, s.destination),
        ).fetchone()
        assert row == (404, {"detail": "Resource not found"})
    with s.database.connect(user="payment_migrator") as connection:
        assert connection.execute(
            "SELECT octet_length(request_hash) FROM payment.idempotency_records"
        ).fetchone() == (32,)
    assert counts(s.database) == (0, 0, 0, 1)


@pytest.mark.parametrize("table", ["transfers", "idempotency_records"])
@pytest.mark.parametrize("operation", ["update", "delete", "truncate"])
def test_transfer_and_key_retention_even_for_owner(
    scenario: Scenario, table: str, operation: str
) -> None:
    assert scenario.post("retained")[0] == 201
    statement = {
        "update": f"UPDATE payment.{table} SET user_id=user_id",
        "delete": f"DELETE FROM payment.{table}",
        "truncate": f"TRUNCATE payment.{table} CASCADE",
    }[operation]
    with (
        scenario.database.connect(user="payment_migrator") as connection,
        pytest.raises(CheckViolation),
    ):
        connection.execute(statement)
    assert counts(scenario.database) == (1, 1, 1, 1)


@pytest.mark.parametrize("failure", ["credit", "audit", "response", "commit"])
def test_failure_rolls_back_key_transfer_journal_balances_and_audit(
    scenario: Scenario, failure: str
) -> None:
    s = scenario
    table, event, condition = {
        "credit": ("ledger_entries", "INSERT", "NEW.amount_minor > 0"),
        "audit": ("audit_events", "INSERT", "NEW.event_type='transfer_created'"),
        "response": ("idempotency_records", "UPDATE", "true"),
        "commit": ("transfers", "INSERT", "true"),
    }[failure]
    with s.database.connect(user="payment_migrator") as connection:
        connection.execute(f"""
            CREATE FUNCTION payment.reject_m4() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF {condition} THEN RAISE EXCEPTION 'injected M4' USING ERRCODE='23514';
            END IF; RETURN NEW; END $$;
        """)
        if failure == "commit":
            connection.execute(
                "CREATE CONSTRAINT TRIGGER injected_m4 AFTER INSERT ON payment.transfers "
                "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION payment.reject_m4()"
            )
        else:
            connection.execute(
                f"CREATE TRIGGER injected_m4 BEFORE {event} ON payment.{table} "
                "FOR EACH ROW EXECUTE FUNCTION payment.reject_m4()"
            )
    status, body = s.post("rollback")
    assert status == 503 and body == {"detail": "Service temporarily unavailable"}
    assert counts(s.database) == (0, 0, 0, 0)
    assert balances(s) == (100000, 0)
    with s.database.connect(user="payment_migrator") as connection:
        connection.execute(f"DROP TRIGGER injected_m4 ON payment.{table}")
    assert s.post("rollback")[0] == 201
    assert_clean(s.database)


@pytest.mark.parametrize("case", ["duplicate", "different_keys", "conflicting_payload", "crossed"])
def test_concurrent_transfers(scenario: Scenario, case: str) -> None:
    s = scenario
    barrier = Barrier(2)
    engine = create_database_engine(s.database.settings)
    if case == "crossed":
        opening(s.database, s.bob, s.destination)
    original = TransferRequest.model_validate(s.payload(60000))

    def attempt(index: int) -> service.StoredResponse:
        key = "same" if case in ("duplicate", "conflicting_payload") else f"key-{index}"
        request = original
        owner = s.alice
        if case == "conflicting_payload" and index:
            request = request.model_copy(update={"amount_minor": 50000})
        if case == "crossed" and index:
            owner = s.bob
            request = request.model_copy(
                update={"source_wallet_id": s.destination, "destination_wallet_id": s.source}
            )
        barrier.wait(timeout=5)
        return service.create_transfer(engine, owner, key, request)

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, [0, 1]))
    finally:
        engine.dispose()
    if case == "duplicate":
        assert results[0] == results[1] and results[0].status_code == 201
        assert counts(s.database) == (1, 1, 1, 1)
    elif case == "crossed":
        assert [r.status_code for r in results] == [201, 201]
        assert balances(s) == (100000, 100000)
        assert counts(s.database) == (2, 2, 2, 2)
    else:
        assert sorted(r.status_code for r in results) == [201, 409]
        assert counts(s.database) == (1, 1, 1, 2 if case == "different_keys" else 1)
    assert_clean(s.database)


@pytest.mark.parametrize("winner_commits", [False, True])
def test_duplicate_waits_for_claim_then_recovers(scenario: Scenario, winner_commits: bool) -> None:
    s = scenario
    engine = create_database_engine(s.database.settings)
    barrier = Barrier(2)
    with s.database.connect() as blocker:
        blocker.execute(
            "SELECT * FROM payment.create_transfer(%s,'waiting',%s,%s,1000::bigint,'EUR')",
            (s.alice, s.source, s.destination),
        )
        pid = blocker.info.backend_pid

        def contender() -> service.StoredResponse:
            barrier.wait(timeout=5)
            return service.create_transfer(
                engine, s.alice, "waiting", TransferRequest.model_validate(s.payload())
            )

        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(contender)
                try:
                    barrier.wait(timeout=5)
                    deadline = monotonic() + 2
                    with s.database.connect(user="postgres", autocommit=True) as observer:
                        while not observer.execute(
                            "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                            (pid,),
                        ).fetchone():
                            assert monotonic() < deadline, "duplicate did not wait for claim"
                        assert counts(s.database) == (0, 0, 0, 0)
                finally:
                    if winner_commits:
                        blocker.commit()
                    else:
                        blocker.rollback()
                assert future.result(timeout=5).status_code == 201
        finally:
            engine.dispose()
    assert counts(s.database) == (1, 1, 1, 1)
    assert balances(s) == (99000, 1000)


@pytest.mark.parametrize("sqlstate", ["40001", "40P01"])
def test_transient_local_error_retries_entire_transaction(
    scenario: Scenario, sqlstate: str
) -> None:
    s = scenario
    with s.database.connect(user="payment_migrator") as connection:
        # Sequence increments survive rollback, so first attempt fails once deterministically.
        connection.execute(f"""
            CREATE SEQUENCE payment.retry_probe;
            CREATE FUNCTION payment.fail_once() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF nextval('payment.retry_probe')=1 THEN
                RAISE EXCEPTION 'retry me' USING ERRCODE='{sqlstate}'; END IF; RETURN NEW; END $$;
            CREATE TRIGGER fail_once BEFORE INSERT ON payment.transfers
                FOR EACH ROW EXECUTE FUNCTION payment.fail_once();
        """)
    assert s.post("retry")[0] == 201
    assert counts(s.database) == (1, 1, 1, 1)
    with s.database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT last_value FROM payment.retry_probe").fetchone() == (2,)
    assert_clean(s.database)


def test_retry_exhaustion_is_bounded(scenario: Scenario) -> None:
    s = scenario
    with s.database.connect(user="payment_migrator") as connection:
        connection.execute("""
            CREATE SEQUENCE payment.retry_probe;
            CREATE FUNCTION payment.always_fail() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM nextval('payment.retry_probe');
                RAISE EXCEPTION 'retry me' USING ERRCODE='40001'; END $$;
            CREATE TRIGGER always_fail BEFORE INSERT ON payment.transfers
                FOR EACH ROW EXECUTE FUNCTION payment.always_fail();
        """)
    assert s.post("exhausted")[0] == 503
    assert counts(s.database) == (0, 0, 0, 0)
    with s.database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT last_value FROM payment.retry_probe").fetchone() == (3,)


def test_db_rejects_incomplete_key_and_transfer(scenario: Scenario) -> None:
    s = scenario
    with pytest.raises(CheckViolation), s.database.connect(user="payment_migrator") as connection:
        connection.execute(
            "INSERT INTO payment.idempotency_records(user_id,operation_kind,key,request_hash) "
            "VALUES (%s,'transfer','unfinished',sha256('x'::bytea))",
            (s.alice,),
        )
    with pytest.raises(CheckViolation), s.database.connect(user="payment_migrator") as connection:
        record = connection.execute(
            "INSERT INTO payment.idempotency_records(user_id,operation_kind,key,request_hash,"
            "response_status,response_body) "
            "VALUES (%s,'transfer','orphan',sha256('x'::bytea),409,'{}') RETURNING id",
            (s.alice,),
        ).fetchone()
        assert record is not None
        connection.execute(
            "INSERT INTO payment.transfers(user_id,idempotency_id,source_wallet_id,"
            "destination_wallet_id,currency,amount_minor) "
            "VALUES (%s,%s,%s,%s,'EUR',100)",
            (s.alice, record[0], s.source, s.destination),
        )
    assert counts(s.database) == (0, 0, 0, 0)


@pytest.mark.migration_revision("0003_ledger")
def test_upgrade_retains_openings_and_readiness_requires_m4(database: Database) -> None:
    owner, target = wallet(database)
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    opening(database, owner, target)
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
        with database.connect(user="payment_migrator") as connection:
            connection.execute(
                "REVOKE EXECUTE ON FUNCTION payment.create_transfer("
                "uuid,text,uuid,uuid,bigint,text) FROM payment_app"
            )
        assert client.get("/health/ready").status_code == 503
        database.migrate("downgrade", "0003_ledger")
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
    assert_clean(database)
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (1,)


def test_populated_m4_downgrade_preserves_all_history(scenario: Scenario) -> None:
    assert scenario.post("history")[0] == 201
    with pytest.raises(RuntimeError, match="cannot downgrade retained transfer"):
        scenario.database.migrate("downgrade", "0003_ledger")
    assert counts(scenario.database) == (1, 1, 1, 1)
    assert_clean(scenario.database)


@pytest.mark.parametrize("crossed", [False, True])
def test_account_contention_is_observed_before_release(scenario: Scenario, crossed: bool) -> None:
    s = scenario
    if crossed:
        opening(s.database, s.bob, s.destination)
    low, high = sorted((accounts(s.database, s.source)[0], accounts(s.database, s.destination)[0]))
    engine = create_database_engine(
        s.database.settings.model_copy(update={"db_statement_timeout_ms": 8000})
    )
    barrier = Barrier(3)

    def contender(index: int) -> service.StoredResponse:
        request = TransferRequest.model_validate(s.payload(60000))
        owner = s.alice
        if crossed and index:
            owner = s.bob
            request = request.model_copy(
                update={"source_wallet_id": s.destination, "destination_wallet_id": s.source}
            )
        barrier.wait(timeout=5)
        return service.create_transfer(engine, owner, f"contention-{index}", request)

    try:
        with s.database.connect(user="payment_migrator") as blocker:
            blocker.execute("SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE", (low,))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(contender, index) for index in (0, 1)]
                try:
                    barrier.wait(timeout=5)
                    deadline = monotonic() + 5
                    with s.database.connect(user="postgres", autocommit=True) as observer:
                        while True:
                            row = observer.execute(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() AND wait_event_type='Lock' "
                                "AND query LIKE 'SELECT * FROM payment.create_transfer%'"
                            ).fetchone()
                            if row == (2,):
                                break
                            assert monotonic() < deadline, "both transfers must wait on accounts"
                        # The second may queue behind the first tuple waiter; the first
                        # waits on our blocker. Both must leave the higher account free.
                        with observer.transaction():
                            observer.execute(
                                "SELECT 1 FROM payment.ledger_accounts "
                                "WHERE id=%s FOR UPDATE NOWAIT",
                                (high,),
                            )
                finally:
                    blocker.rollback()
                outcomes = [future.result(timeout=8) for future in futures]
    finally:
        engine.dispose()
    assert sorted(result.status_code for result in outcomes) == (
        [201, 201] if crossed else [201, 409]
    )
    assert balances(s) == ((100000, 100000) if crossed else (40000, 60000))
    assert counts(s.database) == ((2, 2, 2, 2) if crossed else (1, 1, 1, 2))
    assert_clean(s.database)


def test_connection_failure_is_not_blindly_retried(scenario: Scenario) -> None:
    s = scenario
    with s.database.connect(user="payment_migrator") as connection:
        connection.execute("""
            CREATE SEQUENCE payment.no_retry_probe;
            CREATE FUNCTION payment.connection_failure() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN PERFORM nextval('payment.no_retry_probe');
                RAISE EXCEPTION 'connection failure' USING ERRCODE='08006'; END $$;
            CREATE TRIGGER connection_failure BEFORE INSERT ON payment.transfers
                FOR EACH ROW EXECUTE FUNCTION payment.connection_failure();
        """)
    assert s.post("unknown")[0] == 503
    with s.database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT last_value FROM payment.no_retry_probe").fetchone() == (
            1,
        )
    assert counts(s.database) == (0, 0, 0, 0)


@pytest.mark.parametrize("currency", ["USD", "JPY"])
def test_transfers_preserve_each_supported_currency(database: Database, currency: str) -> None:
    owner, source = wallet(database, currency)
    _, destination = wallet(database, currency)
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    opening(database, owner, source)
    engine = create_database_engine(database.settings)
    try:
        response = service.create_transfer(
            engine,
            owner,
            "currency",
            TransferRequest(
                source_wallet_id=source,
                destination_wallet_id=destination,
                amount_minor=1,
                currency=currency,
            ),
        )
        assert response.status_code == 201
        assert response.body["currency"] == currency and response.body["amount_minor"] == 1
    finally:
        engine.dispose()
    assert_clean(database)
