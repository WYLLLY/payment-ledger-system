"""Real PostgreSQL evidence for M3; no mocked monetary state or random sleeps."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import monotonic
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import (
    CheckViolation,
    ForeignKeyViolation,
    InsufficientPrivilege,
    InvalidParameterValue,
    NumericValueOutOfRange,
    UniqueViolation,
)

from payment_ledger.database import create_database_engine
from payment_ledger.ledger.reconciliation import reconcile
from payment_ledger.main import create_app
from scripts.seed_demo import DEMO_USERS, provision_dev_role, seed

from .conftest import Database
from .test_identity_wallets import login, register

pytestmark = pytest.mark.integration


def wallet(database: Database, currency: str = "EUR") -> tuple[UUID, UUID]:
    with database.connect(user="payment_migrator") as connection:
        owner = uuid4()
        connection.execute(
            "INSERT INTO payment.users(id,email,password_hash) VALUES (%s,%s,'$argon2id$test')",
            (owner, f"{owner}@example.com"),
        )
        row = connection.execute(
            "SELECT payment.create_wallet(%s,%s)", (owner, currency)
        ).fetchone()
        assert row is not None
        return owner, UUID(str(row[0]))


@pytest.fixture
def dev(database: Database) -> Database:
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    return database


def opening(database: Database, owner: UUID, target: UUID) -> UUID:
    with database.connect(user="postgres") as connection:
        connection.execute("SET SESSION AUTHORIZATION payment_dev")
        row = connection.execute(
            "SELECT payment.open_fixture_wallet(%s,%s)", (owner, target)
        ).fetchone()
        assert row is not None
        return UUID(str(row[0]))


def accounts(database: Database, target: UUID) -> tuple[UUID, UUID]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT a.id, f.id FROM payment.ledger_accounts a "
            "JOIN payment.ledger_accounts f ON f.currency=a.currency "
            "AND f.account_kind='funding' AND f.wallet_id IS NULL "
            "WHERE a.wallet_id=%s AND a.account_kind='available'",
            (target,),
        ).fetchone()
        assert rows is not None
        return UUID(str(rows[0])), UUID(str(rows[1]))


def assert_clean(database: Database) -> None:
    engine = create_database_engine(database.settings)
    try:
        assert reconcile(engine) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("currency,amount", [("EUR", 100000), ("USD", 100000), ("JPY", 10000)])
def test_opening_is_balanced_projected_audited_and_unique(
    dev: Database, currency: str, amount: int
) -> None:
    owner, target = wallet(dev, currency)
    journal = opening(dev, owner, target)
    available, funding = accounts(dev, target)
    with dev.connect(user="payment_migrator") as connection:
        assert connection.execute(
            "SELECT count(*),sum(amount_minor),count(DISTINCT currency) "
            "FROM payment.ledger_entries WHERE journal_id=%s",
            (journal,),
        ).fetchone() == (2, 0, 1)
        for account, expected in ((available, amount), (funding, -amount)):
            assert connection.execute(
                "SELECT balance_minor FROM payment.ledger_accounts WHERE id=%s", (account,)
            ).fetchone() == (expected,)
        assert connection.execute(
            "SELECT user_id,resource_id FROM payment.audit_events WHERE event_type='ledger_opened'"
        ).fetchall() == [(owner, journal)]
    with pytest.raises(UniqueViolation):
        opening(dev, owner, target)
    assert_clean(dev)


@pytest.mark.parametrize("bad", ["owner", "wallet", "null_owner", "null_wallet"])
def test_fixture_rejects_wrong_owner_or_wallet(dev: Database, bad: str) -> None:
    owner, target = wallet(dev)
    args = {
        "owner": (uuid4(), target),
        "wallet": (owner, uuid4()),
        "null_owner": (None, target),
        "null_wallet": (owner, None),
    }[bad]
    with dev.connect(user="postgres") as connection:
        connection.execute("SET SESSION AUTHORIZATION payment_dev")
        with pytest.raises(InvalidParameterValue):
            connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", args)
    assert_clean(dev)


@pytest.mark.parametrize("amount", [None, 0, -1, 1000000000001])
def test_private_posting_rejects_unbounded_amount(database: Database, amount: int | None) -> None:
    owner, target = wallet(database)
    credit, debit = accounts(database, target)
    with (
        database.connect(user="payment_migrator") as connection,
        pytest.raises(InvalidParameterValue),
    ):
        connection.execute(
            "SELECT payment.post_opening_pair(%s,%s,%s,%s,%s::bigint,'EUR')",
            (owner, target, debit, credit, amount),
        )
    assert_clean(database)


@pytest.mark.parametrize("bad", ["currency", "same", "missing", "reserved", "owner", "clearing"])
def test_private_posting_validates_accounts_currency_and_owner(
    database: Database, bad: str
) -> None:
    owner, target = wallet(database)
    credit, debit = accounts(database, target)
    currency = "EUR"
    if bad == "currency":
        currency = "USD"
    elif bad == "same":
        debit = credit
    elif bad == "missing":
        credit = uuid4()
    elif bad == "owner":
        owner = uuid4()
    else:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM payment.ledger_accounts WHERE currency='EUR' AND account_kind=%s",
                (bad,),
            ).fetchone()
            assert row is not None
            if bad == "reserved":
                credit = UUID(str(row[0]))
            else:
                debit = UUID(str(row[0]))
    with (
        database.connect(user="payment_migrator") as connection,
        pytest.raises(InvalidParameterValue),
    ):
        connection.execute(
            "SELECT payment.post_opening_pair(%s,%s,%s,%s,1::bigint,%s)",
            (owner, target, debit, credit, currency),
        )
    assert_clean(database)


@pytest.mark.parametrize("role", ["payment_app", "payment_worker", "payment_dev"])
def test_roles_cannot_write_ledger_or_call_private_functions(dev: Database, role: str) -> None:
    statements = [
        f"{operation} payment.{table}{suffix}"
        for table in ("ledger_journals", "ledger_entries", "ledger_accounts")
        for operation, suffix in (
            ("INSERT INTO", " DEFAULT VALUES"),
            ("DELETE FROM", ""),
            ("TRUNCATE", ""),
        )
    ] + [
        "UPDATE payment.ledger_accounts SET balance_minor=100",
        "UPDATE payment.ledger_entries SET amount_minor=1",
        "UPDATE payment.ledger_journals SET currency='USD'",
        "SELECT payment.post_opening_pair(NULL,NULL,NULL,NULL,1::bigint,'EUR')",
        "SELECT payment.apply_ledger_entry()",
        "SELECT payment.check_journal_pair()",
        "SET ROLE payment_migrator",
    ]
    if role != "payment_dev":
        statements += ["SELECT payment.open_fixture_wallet(NULL,NULL)", "SET ROLE payment_dev"]
    for statement in statements:
        with dev.connect(user="postgres") as connection:
            connection.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))
            with pytest.raises(InsufficientPrivilege):
                connection.execute(statement)


def test_default_deployment_has_no_funding_grant_and_functions_are_safe(database: Database) -> None:
    # Role may exist cluster-wide from another dev database; migrations never grant it here.
    with database.connect(user="payment_migrator") as connection:
        assert (
            connection.execute("""
            SELECT grantee FROM information_schema.routine_privileges
            WHERE routine_schema='payment' AND routine_name='open_fixture_wallet'
              AND grantee <> 'payment_migrator'
        """).fetchall()
            == []
        )
        rows = connection.execute("""
            SELECT proname, proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='payment' AND prosecdef
        """).fetchall()
        assert rows
        assert all(config == ["search_path=pg_catalog, pg_temp"] for _, config in rows)
        assert (
            connection.execute("""
            SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace,
                LATERAL aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) acl
            WHERE n.nspname='payment' AND acl.grantee=0 AND privilege_type='EXECUTE'
        """).fetchall()
            == []
        )


@pytest.mark.parametrize("table", ["ledger_entries", "ledger_journals"])
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "TRUNCATE"])
def test_ledger_is_immutable_even_for_table_owner(
    dev: Database, table: str, operation: str
) -> None:
    owner, target = wallet(dev)
    opening(dev, owner, target)
    statement = f"{operation} payment.{table}"
    if operation == "UPDATE":
        statement += " SET currency=currency"
    elif operation == "DELETE":
        statement = f"DELETE FROM payment.{table}"
    elif table == "ledger_journals":
        statement += ", payment.ledger_entries"
    with dev.connect(user="payment_migrator") as connection, pytest.raises(CheckViolation):
        connection.execute(statement)
    assert_clean(dev)


def raw_journal(connection: psycopg.Connection[tuple[object, ...]], target: UUID) -> UUID:
    journal = uuid4()
    connection.execute(
        "INSERT INTO payment.ledger_journals(id,kind,opening_wallet_id,currency) "
        "VALUES (%s,'opening',%s,'EUR')",
        (journal, target),
    )
    return journal


@pytest.mark.parametrize("amounts", [[], [-10], [-10, 9], [-10, 5, 5]])
def test_deferred_pair_rejects_empty_single_unbalanced_and_extra_entries(
    database: Database, amounts: list[int]
) -> None:
    _, target = wallet(database)
    credit, debit = accounts(database, target)
    with database.connect() as connection:
        reserved_row = connection.execute(
            "SELECT id FROM payment.ledger_accounts WHERE wallet_id=%s AND account_kind='reserved'",
            (target,),
        ).fetchone()
        assert reserved_row is not None
    with pytest.raises(CheckViolation), database.connect(user="payment_migrator") as connection:
        journal = raw_journal(connection, target)
        for account, amount in zip((debit, credit, reserved_row[0]), amounts, strict=False):
            connection.execute(
                "INSERT INTO payment.ledger_entries VALUES (%s,%s,'EUR',%s)",
                (journal, account, amount),
            )
        # No exception until commit: constraint triggers inspect the complete journal.
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM payment.ledger_accounts WHERE balance_minor<>0"
        ).fetchone() == (0,)
    assert_clean(database)


@pytest.mark.parametrize("case", ["currency", "negative_user", "zero", "bound"])
def test_entry_constraints_roll_back_projection(database: Database, case: str) -> None:
    _, target = wallet(database)
    credit, debit = accounts(database, target)
    error = ForeignKeyViolation if case == "currency" else CheckViolation
    with pytest.raises(error), database.connect(user="payment_migrator") as connection:
        journal = raw_journal(connection, target)
        connection.execute(
            "INSERT INTO payment.ledger_entries VALUES (%s,%s,%s,%s)",
            (
                journal,
                credit if case == "negative_user" else debit,
                "USD" if case == "currency" else "EUR",
                {"currency": -1, "negative_user": -1, "zero": 0, "bound": -1000000000001}[case],
            ),
        )
    assert_clean(database)


@pytest.mark.parametrize("failure", ["second_entry", "audit", "before_commit"])
def test_opening_failure_rolls_back_every_effect(dev: Database, failure: str) -> None:
    owner, target = wallet(dev)
    if failure != "before_commit":
        table = "ledger_entries" if failure == "second_entry" else "audit_events"
        condition = (
            "NEW.amount_minor > 0"
            if failure == "second_entry"
            else "NEW.event_type='ledger_opened'"
        )
        with dev.connect(user="payment_migrator") as connection:
            connection.execute(f"""
                CREATE FUNCTION payment.inject_failure() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN IF {condition} THEN RAISE EXCEPTION 'injected' USING ERRCODE='23514';
                END IF; RETURN NEW; END $$;
                CREATE TRIGGER injected BEFORE INSERT ON payment.{table}
                FOR EACH ROW EXECUTE FUNCTION payment.inject_failure();
            """)
    with pytest.raises(CheckViolation), dev.connect(user="postgres") as connection:
        connection.execute("SET SESSION AUTHORIZATION payment_dev")
        connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", (owner, target))
        if failure == "before_commit":
            raise CheckViolation("injected failure before commit")
    with dev.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_entries").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM payment.ledger_accounts WHERE balance_minor<>0"
        ).fetchone() == (0,)
    with dev.connect(user="payment_migrator") as connection:
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='ledger_opened'"
        ).fetchone() == (0,)


def test_bigint_overflow_aborts_whole_posting(dev: Database) -> None:
    owner, target = wallet(dev)
    credit, debit = accounts(dev, target)
    with dev.connect(user="payment_migrator") as connection:
        connection.execute(
            "UPDATE payment.ledger_accounts SET balance_minor=9223372036854775807 WHERE id=%s",
            (credit,),
        )
    with pytest.raises(NumericValueOutOfRange):
        opening(dev, owner, target)
    with dev.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (0,)
        assert connection.execute(
            "SELECT balance_minor FROM payment.ledger_accounts WHERE id=%s", (debit,)
        ).fetchone() == (0,)


@pytest.mark.parametrize("duplicate", [True, False])
def test_concurrent_openings_preserve_uniqueness_and_shared_funding_balance(
    dev: Database, duplicate: bool
) -> None:
    first = wallet(dev)
    second = first if duplicate else wallet(dev)
    barrier = Barrier(2)

    def attempt(args: tuple[UUID, UUID]) -> str:
        with dev.connect(user="postgres") as connection:
            connection.execute("SET SESSION AUTHORIZATION payment_dev")
            connection.execute("SET statement_timeout='5s'")
            barrier.wait(timeout=5)
            try:
                connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", args)
                connection.commit()
                return "posted"
            except UniqueViolation:
                connection.rollback()
                return "duplicate"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [first, second]))
    assert sorted(results) == (["duplicate", "posted"] if duplicate else ["posted", "posted"])
    with dev.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (
            1 if duplicate else 2,
        )
        assert connection.execute(
            "SELECT balance_minor FROM payment.ledger_accounts "
            "WHERE account_kind='funding' AND currency='EUR'"
        ).fetchone() == (-100000 if duplicate else -200000,)
    assert_clean(dev)


def test_posting_locks_lower_account_before_higher(dev: Database) -> None:
    owner, target = wallet(dev)
    low, high = sorted(accounts(dev, target))
    barrier = Barrier(2)
    with dev.connect(user="payment_migrator") as blocker:
        blocker.execute("SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE", (low,))
        pid = blocker.info.backend_pid

        def attempt() -> None:
            with dev.connect(user="postgres") as connection:
                connection.execute("SET SESSION AUTHORIZATION payment_dev")
                connection.execute("SET statement_timeout='8s'")
                barrier.wait(timeout=5)
                connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", (owner, target))

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(attempt)
            try:
                barrier.wait(timeout=5)
                deadline = monotonic() + 5
                with dev.connect(user="postgres", autocommit=True) as observer:
                    while not observer.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (pid,)
                    ).fetchone():
                        assert monotonic() < deadline, "posting never waited on the lower account"
                    # If posting acquired the higher lock first, this NOWAIT fails.
                    with observer.transaction():
                        observer.execute(
                            "SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE NOWAIT",
                            (high,),
                        )
            finally:
                blocker.rollback()
            future.result(timeout=8)
    assert_clean(dev)


def test_reconciliation_detects_drift_without_repair(dev: Database) -> None:
    owner, target = wallet(dev)
    opening(dev, owner, target)
    credit, _ = accounts(dev, target)
    with dev.connect(user="payment_migrator") as connection:
        connection.execute(
            "UPDATE payment.ledger_accounts SET balance_minor=balance_minor+1 WHERE id=%s",
            (credit,),
        )
    engine = create_database_engine(dev.settings)
    try:
        assert reconcile(engine) == [
            {"issue": "balance_mismatch", "reference": str(credit)},
            {"issue": "currency_not_conserved", "reference": "EUR"},
        ]
        assert reconcile(engine) == reconcile(engine)
    finally:
        engine.dispose()
    with dev.connect() as connection:
        assert connection.execute(
            "SELECT balance_minor FROM payment.ledger_accounts WHERE id=%s", (credit,)
        ).fetchone() == (100001,)


def test_history_is_private_paginated_and_matches_displayed_balance(dev: Database) -> None:
    with TestClient(create_app(dev.settings)) as client:
        owner = UUID(register(client))
        headers = login(client)
        result = client.post("/wallets", headers=headers, json={"currency": "EUR"}).json()
        target = UUID(result["id"])
        path = f"/wallets/{target}/ledger"
        assert client.get(path, headers=headers).json() == []
        journal = opening(dev, owner, target)
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["journal_id"] == str(journal)
        assert entries[0]["amount_minor"] == 100000
        displayed = client.get(f"/wallets/{target}", headers=headers).json()
        assert sum(a["balance_minor"] for a in displayed["accounts"]) == sum(
            e["amount_minor"] for e in entries
        )
        assert client.get(path + "?limit=1&offset=1", headers=headers).json() == []
        assert client.get(path + "?limit=101", headers=headers).status_code == 422
        assert client.get(path).status_code == 401
        register(client, "bob@example.com")
        other = login(client, "bob@example.com")
        denied = client.get(path, headers=other)
        assert denied.status_code == 404
        assert denied.json() == client.get(f"/wallets/{uuid4()}/ledger", headers=other).json()


@pytest.mark.migration_revision("0002_identity_wallets")
def test_m2_m3_upgrade_preserves_existing_wallets_and_readiness(database: Database) -> None:
    owner, target = wallet(database)
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
        with database.connect() as connection:
            assert connection.execute(
                "SELECT user_id FROM payment.wallets WHERE id=%s", (target,)
            ).fetchone() == (owner,)
        database.migrate("downgrade", "0002_identity_wallets")
        assert client.get("/health/ready").status_code == 503
        database.migrate("upgrade", "head")
        assert client.get("/health/ready").status_code == 200
    assert_clean(database)


def test_populated_ledger_cannot_be_downgraded(dev: Database) -> None:
    owner, target = wallet(dev)
    opening(dev, owner, target)
    with pytest.raises(RuntimeError, match="cannot downgrade a populated ledger"):
        dev.migrate("downgrade", "0002_identity_wallets")
    assert_clean(dev)
    with dev.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (1,)


def test_fixed_seed_is_repeatable_and_runs_under_dev_role(database: Database) -> None:
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
        connection.execute("GRANT payment_dev TO payment_migrator WITH SET TRUE")
        seed(connection, "$argon2id$test")
    with database.connect(user="payment_migrator") as connection:
        seed(connection, "$argon2id$different")
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (6,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_entries").fetchone() == (12,)
        assert connection.execute(
            "SELECT count(*) FROM payment.users WHERE password_hash='$argon2id$test'"
        ).fetchone() == (2,)
    assert_clean(database)


def test_concurrent_conditional_debits_cannot_overdraw(dev: Database) -> None:
    # Exercise the projection trigger as schema owner, since runtime has no debit API in M3.
    # Each contender writes a distinct journal; account UPDATE must serialize the debits.
    owner, source = wallet(dev)
    opening(dev, owner, source)
    available, funding = accounts(dev, source)
    targets = [wallet(dev)[1], wallet(dev)[1]]
    barrier = Barrier(2)

    def debit(target: UUID) -> str:
        try:
            with dev.connect(user="payment_migrator") as connection:
                connection.execute("SET statement_timeout='5s'")
                journal = raw_journal(connection, target)
                barrier.wait(timeout=5)
                connection.execute(
                    "INSERT INTO payment.ledger_entries VALUES "
                    "(%s,%s,'EUR',-60000),(%s,%s,'EUR',60000)",
                    (journal, available, journal, funding),
                )
            return "posted"
        except CheckViolation:
            return "insufficient"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(debit, targets)) == ["insufficient", "posted"]
    with dev.connect() as connection:
        assert connection.execute(
            "SELECT balance_minor FROM payment.ledger_accounts WHERE id=%s", (available,)
        ).fetchone() == (40000,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_entries").fetchone() == (4,)
    assert_clean(dev)


def test_reconciliation_reports_corrupt_pair_without_writing(database: Database) -> None:
    _, target = wallet(database)
    # Corruption injection at the expressly excluded schema-owner boundary.
    with database.connect(user="payment_migrator") as connection:
        connection.execute(
            "ALTER TABLE payment.ledger_journals DISABLE TRIGGER journal_requires_pair"
        )
        journal = raw_journal(connection, target)
    engine = create_database_engine(database.settings)
    try:
        assert reconcile(engine) == [{"issue": "invalid_journal", "reference": str(journal)}]
    finally:
        engine.dispose()
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_entries").fetchone() == (0,)


def test_readiness_requires_m3_read_privileges(database: Database) -> None:
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 200
        with database.connect(user="payment_migrator") as connection:
            connection.execute("REVOKE SELECT ON payment.ledger_entries FROM payment_app")
        assert client.get("/health/ready").status_code == 503
        with database.connect(user="payment_migrator") as connection:
            connection.execute("GRANT SELECT ON payment.ledger_entries TO payment_app")
        assert client.get("/health/ready").status_code == 200


def test_seed_acquires_global_locks_before_posting(dev: Database) -> None:
    low = UUID("00000000-0000-0000-0000-000000000003")
    high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    with dev.connect(user="payment_migrator") as connection:
        connection.execute("GRANT payment_dev TO payment_migrator WITH SET TRUE")
        for owner, email in DEMO_USERS:
            connection.execute(
                "INSERT INTO payment.users(id,email,password_hash) VALUES (%s,%s,'$argon2id$test')",
                (owner, email),
            )
            for currency in ("EUR", "USD", "JPY"):
                connection.execute("SELECT payment.create_wallet(%s,%s)", (owner, currency))
        connection.execute(
            "UPDATE payment.ledger_accounts SET id=%s "
            "WHERE account_kind='available' AND wallet_id IN "
            "(SELECT id FROM payment.wallets WHERE user_id=%s AND currency='EUR')",
            (low, DEMO_USERS[1][0]),
        )
        connection.execute(
            "UPDATE payment.ledger_accounts SET id=%s "
            "WHERE account_kind='funding' AND currency='EUR'",
            (high,),
        )
    barrier = Barrier(2)

    def run_seed() -> None:
        with dev.connect(user="payment_migrator") as connection:
            connection.execute("SET statement_timeout='8s'")
            barrier.wait(timeout=5)
            seed(connection, "$argon2id$test")

    with dev.connect(user="payment_migrator") as blocker:
        blocker.execute("SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE", (low,))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(run_seed)
            try:
                barrier.wait(timeout=5)
                deadline = monotonic() + 5
                with dev.connect(user="postgres", autocommit=True) as observer:
                    while not observer.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))",
                        (blocker.info.backend_pid,),
                    ).fetchone():
                        assert monotonic() < deadline, "seed did not wait on lowest account"
                    with observer.transaction():
                        observer.execute(
                            "SELECT 1 FROM payment.ledger_accounts WHERE id=%s FOR UPDATE NOWAIT",
                            (high,),
                        )
            finally:
                blocker.rollback()
            future.result(timeout=8)
    with dev.connect() as connection:
        assert connection.execute("SELECT count(*) FROM payment.ledger_journals").fetchone() == (6,)
    assert_clean(dev)


def test_fixture_provisioning_restores_set_without_runtime_funding(database: Database) -> None:
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
    # Reproduce a role with ADMIN but no SET, as found during clean-cluster smoke.
    with database.connect(user="postgres") as connection:
        grantors = connection.execute("""
            SELECT pg_get_userbyid(m.grantor) FROM pg_auth_members m
            JOIN pg_roles granted ON granted.oid=m.roleid
            JOIN pg_roles member ON member.oid=m.member
            WHERE granted.rolname='payment_dev' AND member.rolname='payment_migrator'
        """).fetchall()
        for (grantor,) in grantors:
            connection.execute(
                sql.SQL(
                    "GRANT payment_dev TO payment_migrator WITH SET FALSE GRANTED BY {}"
                ).format(sql.Identifier(str(grantor)))
            )
    with (
        database.connect(user="payment_migrator") as connection,
        pytest.raises(InsufficientPrivilege),
    ):
        connection.execute("SET LOCAL ROLE payment_dev")
    owner, target = wallet(database)
    with database.connect(user="payment_migrator") as connection:
        provision_dev_role(connection)
        connection.execute("SET LOCAL ROLE payment_dev")
        connection.execute("SELECT payment.open_fixture_wallet(%s,%s)", (owner, target))
    with database.connect() as connection:
        assert connection.execute(
            """
            SELECT balance_minor FROM payment.ledger_accounts
            WHERE wallet_id=%s AND account_kind='available'
        """,
            (target,),
        ).fetchone() == (100000,)
        assert connection.execute("""
            SELECT has_function_privilege('payment_app',
                'payment.open_fixture_wallet(uuid,uuid)','EXECUTE'),
                has_function_privilege('payment_worker',
                'payment.open_fixture_wallet(uuid,uuid)','EXECUTE')
        """).fetchone() == (False, False)
    assert_clean(database)
