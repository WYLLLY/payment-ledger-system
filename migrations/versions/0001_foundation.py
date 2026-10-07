"""Create restricted runtime roles and the empty application schema.

The migrator login and database must first be provisioned by an administrator.
Roles are cluster-wide and intentionally survive downgrade to base.
"""

from alembic import op
from sqlalchemy import text

revision: str = "0001_foundation"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # A dedicated cluster is expected. Never silently adopt a privileged role.
    op.execute("""
        DO $$
        DECLARE role_name text;
        BEGIN
            FOREACH role_name IN ARRAY ARRAY['payment_app', 'payment_worker'] LOOP
                IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = role_name) THEN
                    EXECUTE format(
                        'CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE '
                        'NOINHERIT NOREPLICATION NOBYPASSRLS', role_name
                    );
                END IF;
                IF EXISTS (
                    SELECT FROM pg_roles r WHERE r.rolname = role_name
                    AND (r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolinherit
                         OR r.rolreplication OR r.rolbypassrls
                         OR (role_name = 'payment_worker' AND r.rolcanlogin)
                         OR EXISTS (SELECT FROM pg_auth_members m WHERE m.member = r.oid))
                ) THEN
                    RAISE EXCEPTION 'Unsafe attributes or memberships for role %', role_name;
                END IF;
            END LOOP;
        END $$
    """)
    op.execute("CREATE SCHEMA payment AUTHORIZATION payment_migrator")
    op.execute("REVOKE ALL ON SCHEMA public FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA payment FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA payment TO payment_app, payment_worker")
    # Function EXECUTE is public by default; revoke globally for this creator.
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    database = op.get_bind().scalar(text("SELECT current_database()"))
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(str(database))
    op.execute(f"REVOKE ALL ON DATABASE {quoted} FROM PUBLIC")
    op.execute(f"GRANT CONNECT ON DATABASE {quoted} TO payment_app, payment_worker")


def downgrade() -> None:
    op.execute("DROP SCHEMA payment")
    database = op.get_bind().scalar(text("SELECT current_database()"))
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(str(database))
    op.execute(f"REVOKE CONNECT ON DATABASE {quoted} FROM payment_app, payment_worker")
    # Keep restrictive defaults and cluster roles: other databases may use them.
