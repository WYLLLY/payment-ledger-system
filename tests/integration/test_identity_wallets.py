import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import CheckViolation, ForeignKeyViolation, InsufficientPrivilege

from payment_ledger.main import create_app

from .conftest import Database

pytestmark = pytest.mark.integration
PASSWORD = "dummy-correct-horse-battery"


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    with TestClient(create_app(database.settings)) as api:
        yield api


def register(client: TestClient, email: str = "alice@example.com") -> str:
    response = client.post("/auth/register", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def login(client: TestClient, email: str = "alice@example.com") -> dict[str, str]:
    response = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_identity_lifecycle_and_secret_storage(database: Database, client: TestClient) -> None:
    user_id = register(client, "Alice@EXAMPLE.COM")
    duplicate = client.post(
        "/auth/register", json={"email": "alice@example.com", "password": PASSWORD}
    )
    assert duplicate.status_code == 409
    headers = login(client)
    token = headers["Authorization"].split()[1]
    response = client.get("/users/me", headers=headers)
    assert response.status_code == 200
    assert response.json()["id"] == user_id
    assert set(response.json()) == {"id", "email", "created_at"}
    assert PASSWORD not in response.text
    with database.connect(user="payment_migrator") as connection:
        row = connection.execute("SELECT password_hash FROM payment.users").fetchone()
        assert row is not None and str(row[0]).startswith("$argon2id$")
        row = connection.execute("SELECT token_hash FROM payment.sessions").fetchone()
        assert row == (hashlib.sha256(token.encode()).digest(),)
        snapshot = str(
            connection.execute("SELECT row_to_json(s) FROM payment.sessions s").fetchall()
        )
        assert token not in snapshot
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/users/me", headers=headers).status_code == 401
    assert client.post("/auth/logout", headers=headers).status_code == 401
    # A fresh session remains independent of the revoked one.
    assert client.get("/users/me", headers=login(client)).status_code == 200
    with database.connect(user="payment_migrator") as connection:
        events = connection.execute("SELECT event_type FROM payment.audit_events").fetchall()
        assert ("user_registered",) in events
        assert ("session_revoked",) in events
        assert str(events).count("session_revoked") == 1
        assert token not in str(events) and PASSWORD not in str(events)


def test_invalid_login_is_generic_and_audited(database: Database, client: TestClient) -> None:
    register(client)
    responses = [
        client.post("/auth/login", json={"email": email, "password": "wrong"})
        for email in ("alice@example.com", "missing@example.com")
    ]
    assert all(response.status_code == 401 for response in responses)
    assert responses[0].json() == responses[1].json()
    assert responses[0].headers["WWW-Authenticate"] == "Bearer"
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT count(*) FROM payment.sessions").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='login_failed'"
        ).fetchone() == (2,)


@pytest.mark.parametrize(
    "payload",
    [
        {"email": "invalid", "password": PASSWORD},
        {"email": "a@example.com", "password": "secret"},
        {"email": "a@example.com", "password": "x" * 129},
        {"email": "a@example.com", "password": PASSWORD, "is_admin": True},
    ],
)
def test_invalid_registration_never_reflects_password(
    client: TestClient, payload: dict[str, object]
) -> None:
    response = client.post("/auth/register", json=payload)
    assert response.status_code == 422
    assert str(payload["password"]) not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_expired_session_cannot_authenticate_or_logout(
    database: Database, client: TestClient
) -> None:
    register(client)
    headers = login(client)
    with database.connect(user="payment_migrator") as connection:
        connection.execute(
            "UPDATE payment.sessions SET created_at=statement_timestamp()-interval '2 hours', "
            "expires_at=statement_timestamp()-interval '1 hour'"
        )
    for path in ("/users/me", "/wallets"):
        assert client.get(path, headers=headers).status_code == 401
    assert client.post("/auth/logout", headers=headers).status_code == 401


def test_authentication_required_and_invalid_bearer(client: TestClient) -> None:
    for headers in (
        {},
        {"Authorization": "Bearer invalid"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer " + "a" * 43},
    ):
        assert client.get("/users/me", headers=headers).status_code == 401
        assert client.get("/wallets", headers=headers).status_code == 401
        assert client.get(f"/wallets/{uuid4()}", headers=headers).status_code == 401
        assert client.post("/wallets", headers=headers, json={"currency": "EUR"}).status_code == 401


def test_wallets_are_private_and_have_exactly_two_zero_accounts(
    database: Database,
    client: TestClient,
) -> None:
    register(client)
    alice = login(client)
    register(client, "bob@example.com")
    bob = login(client, "bob@example.com")
    wallet_ids = []
    for currency in ("EUR", "USD", "JPY"):
        response = client.post("/wallets", json={"currency": currency}, headers=alice)
        assert response.status_code == 201, response.text
        wallet = response.json()
        wallet_ids.append(wallet["id"])
        assert wallet["currency"] == currency
        assert {a["account_kind"] for a in wallet["accounts"]} == {"available", "reserved"}
        assert len(wallet["accounts"]) == 2
        assert all(a["balance_minor"] == 0 for a in wallet["accounts"])
        assert client.get(f"/wallets/{wallet['id']}", headers=alice).json() == wallet
        other = client.get(f"/wallets/{wallet['id']}", headers=bob)
        missing = client.get(f"/wallets/{uuid4()}", headers=bob)
        assert other.status_code == missing.status_code == 404
        assert other.json() == missing.json()
    assert len(client.get("/wallets", headers=alice).json()) == 3
    assert client.get("/wallets", headers=bob).json() == []
    assert client.post("/wallets", json={"currency": "EUR"}, headers=alice).status_code == 409
    assert client.post("/wallets", json={"currency": "GBP"}, headers=alice).status_code == 422
    assert (
        client.post(
            "/wallets", json={"currency": "EUR", "user_id": str(uuid4())}, headers=alice
        ).status_code
        == 422
    )
    assert client.post("/wallets", json={"currency": "EUR"}, headers=bob).status_code == 201
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute(
            "SELECT count(*), sum(balance_minor) FROM payment.ledger_accounts"
        ).fetchone() == (8, 0)
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='wallet_created'"
        ).fetchone() == (4,)


def test_concurrent_registration_has_one_winner(database: Database, client: TestClient) -> None:
    barrier = Barrier(2)

    def attempt() -> int:
        barrier.wait(timeout=10)
        return client.post(
            "/auth/register", json={"email": "race@example.com", "password": PASSWORD}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [201, 409]
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT count(*) FROM payment.users").fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM payment.audit_events").fetchone() == (1,)


def test_concurrent_wallet_creation_is_atomic(database: Database, client: TestClient) -> None:
    register(client)
    headers = login(client)
    barrier = Barrier(4)

    def attempt() -> int:
        barrier.wait(timeout=10)
        return client.post("/wallets", json={"currency": "EUR"}, headers=headers).status_code

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(attempt) for _ in range(4)]
        assert sorted(future.result(timeout=20) for future in futures) == [201, 409, 409, 409]
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT count(*) FROM payment.wallets").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*), sum(balance_minor) FROM payment.ledger_accounts"
        ).fetchone() == (2, 0)
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='wallet_created'"
        ).fetchone() == (1,)


def test_wallet_partial_failure_rolls_back_all_state(
    database: Database, client: TestClient
) -> None:
    register(client)
    headers = login(client)
    with database.connect(user="payment_migrator") as connection:
        connection.execute("""
            CREATE FUNCTION payment.reject_reserved() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.account_kind = 'reserved' THEN RAISE EXCEPTION 'injected failure'; END IF;
                RETURN NEW;
            END $$;
            CREATE TRIGGER reject_reserved BEFORE INSERT ON payment.ledger_accounts
            FOR EACH ROW EXECUTE FUNCTION payment.reject_reserved();
        """)
    response = client.post("/wallets", headers=headers, json={"currency": "EUR"})
    assert response.status_code == 503
    assert "injected" not in response.text
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT count(*) FROM payment.wallets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_accounts").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='wallet_created'"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER reject_reserved ON payment.ledger_accounts")
    assert client.post("/wallets", headers=headers, json={"currency": "EUR"}).status_code == 201


def test_database_pair_currency_and_nonnegative_constraints(
    database: Database, client: TestClient
) -> None:
    user_id = register(client)
    with pytest.raises(CheckViolation), database.connect(user="payment_migrator") as connection:
        connection.execute(
            "INSERT INTO payment.wallets(user_id,currency) VALUES (%s,'EUR')", (user_id,)
        )
    headers = login(client)
    wallet = client.post("/wallets", headers=headers, json={"currency": "EUR"}).json()
    with pytest.raises(CheckViolation), database.connect(user="payment_migrator") as connection:
        connection.execute(
            "DELETE FROM payment.ledger_accounts WHERE id=%s", (wallet["accounts"][0]["id"],)
        )
    with database.connect(user="payment_migrator") as connection, pytest.raises(CheckViolation):
        connection.execute("UPDATE payment.ledger_accounts SET balance_minor=-1")
    with (
        database.connect(user="payment_migrator") as connection,
        pytest.raises(ForeignKeyViolation),
    ):
        connection.execute("UPDATE payment.ledger_accounts SET currency='USD'")
    with database.connect() as connection, pytest.raises(ForeignKeyViolation):
        connection.execute("SELECT payment.create_wallet(%s,'USD')", (uuid4(),))
    with database.connect() as connection, pytest.raises(CheckViolation):
        connection.execute("SELECT payment.create_wallet(%s,'GBP')", (UUID(user_id),))


def test_rate_limit_counts_failures_and_ignores_forwarded_headers(database: Database) -> None:
    settings = database.settings.model_copy(
        update={"auth_rate_limit": 2, "auth_rate_window_seconds": 3600}
    )
    # Pin the stored window beyond the test duration to avoid wall-clock boundary races.
    with database.connect(user="payment_migrator") as connection:
        connection.execute(
            "INSERT INTO payment.auth_rate_limits "
            "VALUES (%s, extract(epoch FROM now())::bigint, 1)",
            (hashlib.sha256(b"testclient").digest(),),
        )
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/auth/login", json={"email": "missing@example.com", "password": "wrong"}
        )
        assert response.status_code == 401
        response = client.post(
            "/auth/register",
            json={"email": "b@example.com", "password": PASSWORD},
            headers={"X-Forwarded-For": "192.0.2.1"},
        )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0
        assert client.post("/auth/logout").status_code == 401
        with database.connect(user="payment_migrator") as connection:
            connection.execute("UPDATE payment.auth_rate_limits SET window_start=0")
        assert (
            client.post(
                "/auth/register", json={"email": "b@example.com", "password": PASSWORD}
            ).status_code
            == 201
        )


@pytest.mark.parametrize("rollover_before_login", [False, True])
def test_logout_revokes_session_when_anonymous_auth_quota_is_exhausted(
    database: Database,
    rollover_before_login: bool,
) -> None:
    settings = database.settings.model_copy(
        update={"auth_rate_limit": 2, "auth_rate_window_seconds": 3600}
    )
    with TestClient(create_app(settings)) as client:
        register(client)
        if rollover_before_login:
            # Expire registration's window without waiting for the wall clock.
            with database.connect(user="payment_migrator") as connection:
                connection.execute("UPDATE payment.auth_rate_limits SET window_start=0")
        headers = login(client)
        with database.connect(user="payment_migrator") as connection:
            if rollover_before_login:
                assert connection.execute(
                    "SELECT attempts FROM payment.auth_rate_limits"
                ).fetchone() == (1,)
            # Exhaust a stable window even if registration and login straddled one.
            connection.execute(
                "UPDATE payment.auth_rate_limits "
                "SET window_start=(extract(epoch FROM now())::bigint / 3600 + 1) * 3600, "
                "attempts=%s",
                (settings.auth_rate_limit,),
            )
        assert (
            client.post(
                "/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
            ).status_code
            == 429
        )
        assert (
            client.post(
                "/auth/register", json={"email": "new@example.com", "password": PASSWORD}
            ).status_code
            == 429
        )
        with database.connect(user="payment_migrator") as connection:
            attempts_before_logout = connection.execute(
                "SELECT attempts FROM payment.auth_rate_limits"
            ).fetchone()

        assert client.post("/auth/logout", headers=headers).status_code == 204
        assert client.get("/users/me", headers=headers).status_code == 401
        assert client.get("/wallets", headers=headers).status_code == 401
        assert client.post("/auth/logout", headers=headers).status_code == 401

        with database.connect(user="payment_migrator") as connection:
            assert (
                connection.execute("SELECT attempts FROM payment.auth_rate_limits").fetchone()
                == attempts_before_logout
            )
            assert connection.execute(
                "SELECT count(*) FROM payment.audit_events WHERE event_type = 'session_revoked'"
            ).fetchone() == (1,)


def test_rate_limit_atomic_across_app_instances(database: Database) -> None:
    settings = database.settings.model_copy(
        update={"auth_rate_limit": 3, "auth_rate_window_seconds": 3600}
    )
    with database.connect(user="payment_migrator") as connection:
        connection.execute(
            "INSERT INTO payment.auth_rate_limits "
            "VALUES (%s, extract(epoch FROM now())::bigint, 1)",
            (hashlib.sha256(b"testclient").digest(),),
        )
    with TestClient(create_app(settings)) as first, TestClient(create_app(settings)) as second:
        barrier = Barrier(4)

        def attempt(index: int) -> int:
            barrier.wait(timeout=10)
            return (
                (first if index % 2 else second)
                .post("/auth/login", json={"email": "n@example.com", "password": "wrong"})
                .status_code
            )

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(attempt, index) for index in range(4)]
            assert sorted(future.result(timeout=20) for future in futures) == [401, 401, 429, 429]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO payment.wallets(user_id,currency) VALUES (gen_random_uuid(),'EUR')",
        "UPDATE payment.ledger_accounts SET balance_minor=100",
        "INSERT INTO payment.ledger_accounts(wallet_id,currency,account_kind) "
        "VALUES (gen_random_uuid(),'EUR','available')",
        "DELETE FROM payment.ledger_accounts",
        "TRUNCATE payment.ledger_accounts",
        "UPDATE payment.sessions SET revoked_at=NULL",
        "DELETE FROM payment.sessions",
        "UPDATE payment.users SET password_hash='forged'",
        "SELECT * FROM payment.audit_events",
        "UPDATE payment.audit_events SET event_type='login_failed'",
        "TRUNCATE payment.audit_events",
        "SELECT * FROM payment.auth_rate_limits",
        "UPDATE payment.auth_rate_limits SET attempts=1",
        "SELECT payment.assert_wallet_pair(gen_random_uuid())",
    ],
)
def test_runtime_cannot_bypass_write_boundaries(database: Database, statement: str) -> None:
    with database.connect() as connection, pytest.raises(InsufficientPrivilege):
        connection.execute(statement)


def test_worker_and_public_have_no_identity_function_privileges(database: Database) -> None:
    with database.connect(user="payment_migrator") as connection:
        rows = connection.execute("""
            SELECT p.proname, p.prosecdef, p.proconfig,
                   has_function_privilege('payment_worker', p.oid, 'EXECUTE'),
                   EXISTS (SELECT FROM aclexplode(p.proacl) a WHERE a.grantee=0)
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname='payment'
        """).fetchall()
        assert len(rows) == 5
        for _, security_definer, config, worker_execute, public_grant in rows:
            assert security_definer
            assert config == ["search_path=pg_catalog, pg_temp"]
            assert not worker_execute and not public_grant
    with database.connect(user="postgres") as connection:
        connection.execute("SET SESSION AUTHORIZATION payment_worker")
        with pytest.raises(InsufficientPrivilege):
            connection.execute("SELECT * FROM payment.users")


@pytest.mark.migration_revision("0001_foundation")
def test_upgrade_from_approved_m1_and_downgrade(database: Database) -> None:
    database.migrate("upgrade", "head")
    with TestClient(create_app(database.settings)) as client:
        register(client)
        assert client.get("/users/me", headers=login(client)).status_code == 200
    database.migrate("downgrade", "0001_foundation")
    with database.connect(user="payment_migrator") as connection:
        assert (
            connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='payment'"
            ).fetchall()
            == []
        )
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0001_foundation",
        )
    database.migrate("upgrade", "head")


def test_corrupt_hash_and_validation_errors_do_not_disclose_secrets(
    database: Database,
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    register(client)
    with database.connect(user="payment_migrator") as connection:
        connection.execute("UPDATE payment.users SET password_hash='$argon2id$corrupt'")
    response = client.post("/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert response.status_code == 503
    assert "corrupt" not in response.text + caplog.text
    assert PASSWORD not in response.text + caplog.text
    # An unknown key is also attacker-controlled and must not be echoed as a location.
    response = client.post(
        "/auth/register", json={"email": "a@example.com", "password": PASSWORD, PASSWORD: "extra"}
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text


@pytest.mark.parametrize(
    "event", ["user_registered", "login_succeeded", "session_revoked", "wallet_created"]
)
def test_audit_failure_rolls_back_business_change(
    database: Database,
    client: TestClient,
    event: str,
) -> None:
    register(client)
    headers = login(client)
    with database.connect(user="payment_migrator") as connection:
        connection.execute(
            sql.SQL("""
            CREATE FUNCTION payment.reject_audit() RETURNS trigger LANGUAGE plpgsql AS $fn$
            BEGIN
                IF NEW.event_type = {} THEN RAISE EXCEPTION 'audit unavailable'; END IF;
                RETURN NEW;
            END $fn$;
            CREATE TRIGGER reject_audit BEFORE INSERT ON payment.audit_events
            FOR EACH ROW EXECUTE FUNCTION payment.reject_audit();
        """).format(sql.Literal(event))
        )
    if event == "user_registered":
        response = client.post(
            "/auth/register", json={"email": "new@example.com", "password": PASSWORD}
        )
    elif event == "login_succeeded":
        response = client.post(
            "/auth/login", json={"email": "alice@example.com", "password": PASSWORD}
        )
    elif event == "session_revoked":
        response = client.post("/auth/logout", headers=headers)
    else:
        response = client.post("/wallets", headers=headers, json={"currency": "EUR"})
    assert response.status_code == 503
    assert "audit unavailable" not in response.text
    assert client.get("/users/me", headers=headers).status_code == 200
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT count(*) FROM payment.users").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM payment.sessions WHERE revoked_at IS NULL"
        ).fetchone() == (1,)
        assert connection.execute("SELECT count(*) FROM payment.wallets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM payment.ledger_accounts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM payment.audit_events").fetchone() == (2,)


def test_concurrent_logout_revokes_once(database: Database, client: TestClient) -> None:
    register(client)
    headers = login(client)
    barrier = Barrier(2)

    def attempt() -> int:
        barrier.wait(timeout=10)
        return client.post("/auth/logout", headers=headers).status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(attempt) for _ in range(2)]
        assert sorted(future.result(timeout=20) for future in futures) == [204, 401]
    assert client.get("/users/me", headers=headers).status_code == 401
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute(
            "SELECT count(*) FROM payment.audit_events WHERE event_type='session_revoked'"
        ).fetchone() == (1,)
