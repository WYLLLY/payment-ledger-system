"""Restricted operations, retained operator audit and financial admission counters."""

from alembic import op
from sqlalchemy import text

revision: str = "0006_resilience"
down_revision: str | None = "0005_payments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(SCHEMA)
    database = op.get_bind().scalar(text("SELECT current_database()"))
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(str(database))
    op.execute(f"GRANT CONNECT ON DATABASE {quoted} TO payment_operator")
    op.execute(FUNCTIONS)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM payment.operator_events) THEN
                RAISE EXCEPTION 'cannot downgrade retained operator audit';
            END IF;
        END $$;
        DROP FUNCTION payment.operator_action(uuid,text,text,uuid);
        DROP FUNCTION payment.take_financial_slot(uuid,text,text,integer,integer);
        DROP TABLE payment.operator_events;
        DROP TABLE payment.financial_rate_limits;
        DROP TRIGGER payment_audit_correlation ON payment.audit_events;
        DROP FUNCTION payment.correlate_payment_audit();
        ALTER TABLE payment.audit_events DROP COLUMN correlation_id;
        REVOKE SELECT ON payment.payments, payment.payment_outbox, payment.ledger_accounts,
            payment.ledger_journals, payment.ledger_entries FROM payment_operator;
        REVOKE USAGE ON SCHEMA payment FROM payment_operator;
    """)
    database = op.get_bind().scalar(text("SELECT current_database()"))
    quoted = op.get_bind().dialect.identifier_preparer.quote_identifier(str(database))
    op.execute(f"REVOKE CONNECT ON DATABASE {quoted} FROM payment_operator")


SCHEMA = """
    DO $$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='payment_operator') THEN
            CREATE ROLE payment_operator NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB
                NOCREATEROLE NOREPLICATION NOBYPASSRLS;
        END IF;
        IF EXISTS (SELECT FROM pg_roles r WHERE r.rolname='payment_operator' AND
            (r.rolcanlogin OR r.rolinherit OR r.rolsuper OR r.rolcreatedb OR r.rolcreaterole
             OR r.rolreplication OR r.rolbypassrls OR EXISTS (
                SELECT FROM pg_auth_members WHERE member=r.oid))) THEN
            RAISE EXCEPTION 'Unsafe operator role';
        END IF;
    END $$;
    GRANT USAGE ON SCHEMA payment TO payment_operator;
    GRANT SELECT ON payment.payments, payment.payment_outbox, payment.ledger_accounts,
        payment.ledger_journals, payment.ledger_entries TO payment_operator;
    ALTER TABLE payment.audit_events ADD COLUMN correlation_id uuid
        DEFAULT nullif(current_setting('payment.correlation_id',true),'')::uuid;
    CREATE FUNCTION payment.correlate_payment_audit() RETURNS trigger
    LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
    BEGIN
        IF NEW.correlation_id IS NULL AND NEW.event_type IN (
            'payment_created','payment_succeeded','payment_failed','payment_needs_review'
        ) THEN NEW.correlation_id:=NEW.resource_id; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER payment_audit_correlation BEFORE INSERT ON payment.audit_events
        FOR EACH ROW EXECUTE FUNCTION payment.correlate_payment_audit();
    REVOKE ALL ON FUNCTION payment.correlate_payment_audit()
        FROM PUBLIC,payment_app,payment_worker,payment_operator;
    CREATE TABLE payment.financial_rate_limits (
        user_id uuid NOT NULL REFERENCES payment.users(id),
        operation_kind text NOT NULL CHECK (operation_kind IN ('transfer','payment')),
        window_start bigint NOT NULL,
        attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 1001),
        PRIMARY KEY(user_id,operation_kind)
    );
    CREATE TABLE payment.operator_events (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        payment_id uuid NOT NULL REFERENCES payment.payments(id),
        actor name NOT NULL,
        action text NOT NULL CHECK (action IN ('lookup','retry')),
        result text NOT NULL CHECK (result IN ('requested','scheduled','busy','terminal')),
        reason text NOT NULL CHECK (length(reason) BETWEEN 1 AND 200
                                   AND reason ~ '^[a-zA-Z0-9 ._:#/-]+$'),
        correlation_id uuid NOT NULL,
        occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TRIGGER immutable_operator_events BEFORE UPDATE OR DELETE ON payment.operator_events
        FOR EACH ROW EXECUTE FUNCTION payment.reject_ledger_mutation();
    CREATE TRIGGER retained_operator_events BEFORE TRUNCATE ON payment.operator_events
        FOR EACH STATEMENT EXECUTE FUNCTION payment.reject_ledger_mutation();
    GRANT SELECT ON payment.operator_events TO payment_operator;
"""

FUNCTIONS = """
    CREATE FUNCTION payment.take_financial_slot(
        p_owner uuid,p_kind text,p_key text,p_limit integer,p_window integer
    ) RETURNS integer
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    DECLARE v_now bigint; v_start bigint; v_attempts integer;
    BEGIN
        IF p_owner IS NULL OR p_kind IS NULL OR p_kind NOT IN ('transfer','payment')
           OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 128 OR p_key !~ '^[!-~]+$'
           OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000
           OR p_window IS NULL OR p_window NOT BETWEEN 1 AND 3600 THEN
            RAISE EXCEPTION 'invalid financial rate parameters' USING ERRCODE='22023';
        END IF;
        -- Already committed operations always retain replay/conflict semantics at the limit.
        IF EXISTS (SELECT FROM payment.idempotency_records WHERE user_id=p_owner
                   AND operation_kind=p_kind AND key=p_key AND response_status IS NOT NULL)
        THEN RETURN 0; END IF;
        v_now:=floor(extract(epoch FROM clock_timestamp()))::bigint;
        v_start:=(v_now / p_window) * p_window;
        INSERT INTO payment.financial_rate_limits AS r(user_id,operation_kind,window_start,attempts)
            VALUES(p_owner,p_kind,v_start,1)
            ON CONFLICT(user_id,operation_kind) DO UPDATE SET
                window_start=greatest(r.window_start,EXCLUDED.window_start),
                attempts=CASE WHEN r.window_start<EXCLUDED.window_start THEN 1
                              ELSE least(r.attempts+1,1001) END
            RETURNING attempts,window_start INTO v_attempts,v_start;
        RETURN CASE WHEN v_attempts<=p_limit THEN 0
                    ELSE greatest(1,least(p_window,v_start+p_window-v_now))::integer END;
    END $$;

    CREATE FUNCTION payment.operator_action(
        p_id uuid,p_action text,p_reason text,p_correlation uuid)
    RETURNS text
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
    DECLARE o payment.payment_outbox; v_result text;
    BEGIN
        IF p_id IS NULL OR p_action IS NULL OR p_action NOT IN ('lookup','retry')
           OR p_reason IS NULL OR length(p_reason) NOT BETWEEN 1 AND 200
           OR p_reason !~ '^[a-zA-Z0-9 ._:#/-]+$' OR p_correlation IS NULL THEN
            RAISE EXCEPTION 'invalid operator action' USING ERRCODE='22023';
        END IF;
        -- Same queue-first locking order as claim/finish; never steal a live lease.
        SELECT * INTO o FROM payment.payment_outbox WHERE payment_id=p_id FOR UPDATE;
        IF NOT FOUND THEN RAISE EXCEPTION 'payment not found' USING ERRCODE='P0002'; END IF;
        IF p_action='lookup' THEN v_result:='requested';
        ELSIF o.done THEN v_result:='terminal';
        ELSIF o.lease_until>clock_timestamp() THEN v_result:='busy';
        ELSE
            UPDATE payment.payment_outbox SET next_attempt_at=clock_timestamp()
                WHERE payment_id=p_id;
            v_result:='scheduled';
        END IF;
        INSERT INTO payment.operator_events(payment_id,actor,action,result,reason,correlation_id)
            VALUES(p_id,session_user,p_action,v_result,p_reason,p_correlation);
        RETURN v_result;
    END $$;
    REVOKE ALL ON FUNCTION payment.take_financial_slot(uuid,text,text,integer,integer),
        payment.operator_action(uuid,text,text,uuid) FROM PUBLIC,payment_app,payment_worker,
        payment_operator;
    GRANT EXECUTE ON FUNCTION payment.take_financial_slot(uuid,text,text,integer,integer)
        TO payment_app;
    GRANT EXECUTE ON FUNCTION payment.operator_action(uuid,text,text,uuid) TO payment_operator;
"""
