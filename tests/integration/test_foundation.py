import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from pydantic import SecretStr
from sqlalchemy import text

from payment_ledger.database import create_database_engine
from payment_ledger.main import create_app

from .conftest import Database

pytestmark = pytest.mark.integration


@pytest.mark.migration_revision("0001_foundation")
def test_fresh_migration_creates_only_foundation(database: Database) -> None:
    with database.connect(user="payment_migrator") as connection:
        assert connection.execute("SELECT version_num FROM public.alembic_version").fetchone() == (
            "0001_foundation",
        )
        assert (
            connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'payment'"
            ).fetchall()
            == []
        )
        assert connection.execute(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'payment'"
        ).fetchone() == ("payment_migrator",)


def test_base_upgrade_repeat_and_downgrade(database: Database) -> None:
    database.migrate("upgrade", "head")
    database.migrate("downgrade", "base")
    with database.connect(user="payment_migrator") as connection:
        assert (
            connection.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'payment'").fetchone()
            is None
        )
        assert connection.execute("SELECT * FROM public.alembic_version").fetchall() == []
    database.migrate("upgrade", "head")
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 200


def test_application_connects_with_restricted_role_and_read_committed(database: Database) -> None:
    engine = create_database_engine(database.settings)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == "payment_app"
            assert connection.scalar(text("SHOW transaction_isolation")) == "read committed"
            assert connection.scalar(text("SHOW statement_timeout")) == "3s"
        with TestClient(create_app(database.settings)) as client:
            response = client.get("/health/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
    finally:
        engine.dispose()


def test_authentication_failure_is_unavailable_and_redacted(
    database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "deliberately-incorrect-test-password"
    settings = database.settings.model_copy(update={"db_password": SecretStr(secret)})
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health/live").status_code == 200
    assert secret not in caplog.text
    assert "password authentication failed" not in caplog.text


def test_readiness_requires_migrated_schema_and_recovers(database: Database) -> None:
    with TestClient(create_app(database.settings)) as client:
        assert client.get("/health/ready").status_code == 200
        with database.connect(user="payment_migrator") as connection:
            connection.execute("ALTER SCHEMA payment RENAME TO unavailable_schema")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert client.get("/health/live").status_code == 200
        with database.connect(user="payment_migrator") as connection:
            connection.execute("ALTER SCHEMA unavailable_schema RENAME TO payment")
        assert client.get("/health/ready").status_code == 200


@pytest.mark.migration_revision("0001_foundation")
def test_readiness_tracks_m2_upgrade_and_downgrade(database: Database) -> None:
    # Every probe uses the runtime role; only the test fixture runs migrations.
    with TestClient(create_app(database.settings)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}

        database.migrate("upgrade", "head")
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        with database.connect(user="payment_migrator") as connection:
            connection.execute(
                "REVOKE EXECUTE ON FUNCTION payment.create_wallet(uuid, text) FROM payment_app"
            )
        assert client.get("/health/ready").status_code == 503
        with database.connect(user="payment_migrator") as connection:
            connection.execute(
                "GRANT EXECUTE ON FUNCTION payment.create_wallet(uuid, text) TO payment_app"
            )
        assert client.get("/health/ready").status_code == 200

        database.migrate("downgrade", "0001_foundation")
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}


@pytest.mark.parametrize("role", ["payment_app", "payment_worker"])
def test_roles_have_no_administrative_attributes_or_memberships(
    database: Database, role: str
) -> None:
    with database.connect(user="payment_migrator") as connection:
        assert (
            connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            == (False,) * 6
        )
        assert (
            connection.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                "WHERE r.rolname = %s",
                (role,),
            ).fetchall()
            == []
        )


@pytest.mark.parametrize(
    "statement",
    [
        "CREATE TABLE payment.forbidden (id integer)",
        "CREATE TABLE public.forbidden (id integer)",
        "CREATE SCHEMA forbidden",
        "CREATE TEMP TABLE forbidden (id integer)",
        "CREATE ROLE forbidden",
        "SET ROLE payment_migrator",
        "SET ROLE payment_worker",
        "SELECT * FROM public.alembic_version",
    ],
)
@pytest.mark.parametrize("role", ["payment_app", "payment_worker"])
def test_runtime_cannot_escalate_or_modify_schema(
    database: Database, statement: str, role: str
) -> None:
    if role == "payment_app":
        with database.connect() as connection, pytest.raises(InsufficientPrivilege):
            connection.execute(statement)
    else:
        # The future worker is NOLOGIN. SET SESSION AUTHORIZATION also removes
        # the administrator's ability to SET ROLE while exercising its privileges.
        with database.connect(user="postgres") as connection:
            connection.execute(sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role)))
            if statement == "SET ROLE payment_worker":
                connection.execute(statement)
            else:
                with pytest.raises(InsufficientPrivilege):
                    connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT * FROM payment.privilege_probe",
        "INSERT INTO payment.privilege_probe VALUES (1)",
        "UPDATE payment.privilege_probe SET id = 2",
        "DELETE FROM payment.privilege_probe",
        "TRUNCATE payment.privilege_probe",
        "SELECT payment.privilege_probe_function()",
    ],
)
@pytest.mark.parametrize("role", ["payment_app", "payment_worker"])
def test_new_objects_are_private_by_default(database: Database, statement: str, role: str) -> None:
    with database.connect(user="payment_migrator") as connection:
        connection.execute("CREATE TABLE payment.privilege_probe (id integer)")
        connection.execute(
            "CREATE FUNCTION payment.privilege_probe_function() RETURNS integer "
            "LANGUAGE sql AS 'SELECT 1'"
        )
    user = "payment_app" if role == "payment_app" else "postgres"
    with database.connect(user=user) as connection:
        if role == "payment_worker":
            connection.execute("SET SESSION AUTHORIZATION payment_worker")
        with pytest.raises(InsufficientPrivilege):
            connection.execute(statement)
