"""Atomic reservations, fenced outbox recovery and exclusive terminal posting."""

from alembic import op

revision: str = "0005_payments"
down_revision: str | None = "0004_transfers"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(SCHEMA)
    op.execute(FUNCTIONS)


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM payment.payments) OR EXISTS (
                SELECT FROM payment.idempotency_records WHERE operation_kind='payment'
            ) THEN RAISE EXCEPTION 'cannot downgrade retained payment history'; END IF;
        END $$;
        DROP FUNCTION payment.finish_payment(uuid,bigint,text);
        DROP FUNCTION payment.claim_payment(integer);
        DROP FUNCTION payment.create_payment(uuid,text,uuid,text,bigint,text);
        DROP TRIGGER payment_complete ON payment.payments;
        DROP FUNCTION payment.check_payment_complete();
        DROP TABLE payment.payment_outbox;
        ALTER TABLE payment.ledger_journals DROP CONSTRAINT ck_journal_operation,
            DROP COLUMN payment_id,
            ADD CONSTRAINT ck_journal_operation CHECK (
                (kind='opening' AND opening_wallet_id IS NOT NULL AND transfer_id IS NULL) OR
                (kind='transfer' AND opening_wallet_id IS NULL AND transfer_id IS NOT NULL)
            );
        DROP TABLE payment.payments;
        DROP FUNCTION payment.protect_payment();
        ALTER TABLE payment.idempotency_records
            DROP CONSTRAINT idempotency_records_operation_kind_check,
            DROP CONSTRAINT idempotency_records_response_status_check,
            ADD CHECK (operation_kind='transfer'),
            ADD CHECK (response_status IN (201,404,409,422));
        ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN ('user_registered','login_succeeded','login_failed','session_revoked',
                          'wallet_created','ledger_opened','transfer_created')
        );
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION payment.check_idempotency_complete() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_record payment.idempotency_records;
        BEGIN
            SELECT * INTO STRICT v_record FROM payment.idempotency_records WHERE id=NEW.id;
            IF v_record.response_status IS NULL OR
               (v_record.response_status=201 AND NOT EXISTS (
                   SELECT FROM payment.transfers WHERE idempotency_id=NEW.id
               )) THEN
                RAISE EXCEPTION 'incomplete financial idempotency' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$;
    """)


SCHEMA = """
    ALTER TABLE payment.idempotency_records
        DROP CONSTRAINT idempotency_records_operation_kind_check,
        DROP CONSTRAINT idempotency_records_response_status_check,
        ADD CHECK (operation_kind IN ('transfer','payment')),
        ADD CHECK (response_status IN (201,202,404,409,422));
    CREATE TABLE payment.payments (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        user_id uuid NOT NULL REFERENCES payment.users(id),
        idempotency_id uuid NOT NULL UNIQUE REFERENCES payment.idempotency_records(id),
        wallet_id uuid NOT NULL,
        recipient text NOT NULL CHECK (length(recipient) BETWEEN 1 AND 128
                                      AND recipient ~ '^[a-zA-Z0-9_.:@-]+$'),
        currency text NOT NULL CHECK (currency IN ('EUR','USD','JPY')),
        amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 1000000000000),
        state text NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending','needs_review','succeeded','failed')),
        created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT statement_timestamp(),
        FOREIGN KEY (wallet_id,currency) REFERENCES payment.wallets(id,currency),
        UNIQUE (id,currency)
    );
    CREATE INDEX ix_payments_owner_created ON payment.payments(user_id,created_at,id);
    CREATE TABLE payment.payment_outbox (
        payment_id uuid PRIMARY KEY REFERENCES payment.payments(id),
        generation bigint NOT NULL DEFAULT 0 CHECK (generation>=0),
        attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
        lease_until timestamptz,
        next_attempt_at timestamptz NOT NULL DEFAULT statement_timestamp(),
        done boolean NOT NULL DEFAULT false,
        CHECK (NOT done OR lease_until IS NULL)
    );
    CREATE INDEX ix_payment_outbox_due ON payment.payment_outbox(next_attempt_at,payment_id)
        WHERE NOT done;
    ALTER TABLE payment.ledger_journals DROP CONSTRAINT ck_journal_operation,
        ADD COLUMN payment_id uuid,
        ADD FOREIGN KEY (payment_id,currency) REFERENCES payment.payments(id,currency),
        ADD CONSTRAINT ck_journal_operation CHECK (
            (kind='opening' AND opening_wallet_id IS NOT NULL
             AND transfer_id IS NULL AND payment_id IS NULL) OR
            (kind='transfer' AND opening_wallet_id IS NULL
             AND transfer_id IS NOT NULL AND payment_id IS NULL) OR
            (kind IN ('reservation','settlement','release') AND payment_id IS NOT NULL
             AND opening_wallet_id IS NULL AND transfer_id IS NULL)
        );
    CREATE UNIQUE INDEX uq_payment_reservation ON payment.ledger_journals(payment_id)
        WHERE kind='reservation';
    CREATE UNIQUE INDEX uq_payment_terminal ON payment.ledger_journals(payment_id)
        WHERE kind IN ('settlement','release');
    ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
    ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
        event_type IN ('user_registered','login_succeeded','login_failed','session_revoked',
                      'wallet_created','ledger_opened','transfer_created','payment_created',
                      'payment_succeeded','payment_failed','payment_needs_review')
    );
    CREATE FUNCTION payment.protect_payment() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
    BEGIN
        IF TG_OP <> 'UPDATE' THEN
            RAISE EXCEPTION 'payment history is retained' USING ERRCODE='23514';
        END IF;
        IF OLD.state IN ('succeeded','failed') OR
           (to_jsonb(OLD)-'state'-'updated_at') IS DISTINCT FROM
           (to_jsonb(NEW)-'state'-'updated_at') OR
           (OLD.state='needs_review' AND NEW.state='pending') THEN
            RAISE EXCEPTION 'invalid payment transition' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER immutable_payment_identity BEFORE UPDATE OR DELETE ON payment.payments
        FOR EACH ROW EXECUTE FUNCTION payment.protect_payment();
    CREATE TRIGGER retained_payments BEFORE TRUNCATE ON payment.payments
        FOR EACH STATEMENT EXECUTE FUNCTION payment.protect_payment();

    CREATE OR REPLACE FUNCTION payment.check_idempotency_complete() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE v payment.idempotency_records;
    BEGIN
        SELECT * INTO STRICT v FROM payment.idempotency_records WHERE id=NEW.id;
        IF v.response_status IS NULL OR
           (v.response_status=201 AND (v.operation_kind<>'transfer' OR NOT EXISTS (
               SELECT FROM payment.transfers WHERE idempotency_id=v.id))) OR
           (v.response_status=202 AND (v.operation_kind<>'payment' OR NOT EXISTS (
               SELECT FROM payment.payments WHERE idempotency_id=v.id))) THEN
            RAISE EXCEPTION 'incomplete financial idempotency' USING ERRCODE='23514';
        END IF;
        RETURN NULL;
    END $$;

    CREATE FUNCTION payment.check_payment_complete() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE p payment.payments; terminal_kind text;
    BEGIN
        SELECT * INTO STRICT p FROM payment.payments WHERE id=NEW.id;
        IF NOT EXISTS (
            SELECT FROM payment.wallets w
            JOIN payment.idempotency_records i ON i.id=p.idempotency_id AND i.user_id=p.user_id
                AND i.operation_kind='payment' AND i.response_status=202
                AND i.response_body->>'id'=p.id::text
            JOIN payment.payment_outbox o ON o.payment_id=p.id
                AND o.done=(p.state IN ('succeeded','failed'))
            JOIN payment.ledger_journals j ON j.payment_id=p.id AND j.kind='reservation'
            JOIN payment.ledger_accounts a ON a.wallet_id=p.wallet_id AND a.account_kind='available'
            JOIN payment.ledger_accounts r ON r.wallet_id=p.wallet_id AND r.account_kind='reserved'
            JOIN payment.ledger_entries d ON d.journal_id=j.id AND d.account_id=a.id
                AND d.amount_minor=-p.amount_minor
            JOIN payment.ledger_entries c ON c.journal_id=j.id AND c.account_id=r.id
                AND c.amount_minor=p.amount_minor
            WHERE w.id=p.wallet_id AND w.user_id=p.user_id
        ) THEN RAISE EXCEPTION 'incomplete payment reservation' USING ERRCODE='23514'; END IF;
        SELECT kind INTO terminal_kind FROM payment.ledger_journals
            WHERE payment_id=p.id AND kind IN ('settlement','release');
        IF p.state IN ('pending','needs_review') THEN
            IF terminal_kind IS NOT NULL THEN
                RAISE EXCEPTION 'unexpected terminal journal' USING ERRCODE='23514';
            END IF;
        ELSIF NOT EXISTS (
            SELECT FROM payment.ledger_journals j
            JOIN payment.ledger_accounts r ON r.wallet_id=p.wallet_id AND r.account_kind='reserved'
            JOIN payment.ledger_accounts dest ON dest.currency=p.currency AND (
                (p.state='succeeded' AND dest.wallet_id IS NULL AND dest.account_kind='clearing') OR
                (p.state='failed' AND dest.wallet_id=p.wallet_id AND dest.account_kind='available'))
            JOIN payment.ledger_entries d ON d.journal_id=j.id AND d.account_id=r.id
                AND d.amount_minor=-p.amount_minor
            JOIN payment.ledger_entries c ON c.journal_id=j.id AND c.account_id=dest.id
                AND c.amount_minor=p.amount_minor
            WHERE j.payment_id=p.id AND j.kind=CASE p.state
                WHEN 'succeeded' THEN 'settlement' ELSE 'release' END
        ) THEN RAISE EXCEPTION 'incomplete payment outcome' USING ERRCODE='23514'; END IF;
        RETURN NULL;
    END $$;
    CREATE CONSTRAINT TRIGGER payment_complete AFTER INSERT OR UPDATE ON payment.payments
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
            payment.check_payment_complete();
    REVOKE ALL ON FUNCTION payment.protect_payment(),payment.check_payment_complete()
        FROM PUBLIC,payment_app,payment_worker;
    GRANT SELECT ON payment.payments TO payment_app;
"""

FUNCTIONS = """
    CREATE FUNCTION payment.create_payment(
        p_owner uuid,p_key text,p_wallet uuid,p_recipient text,p_amount bigint,p_currency text
    ) RETURNS TABLE(status_code integer,body jsonb)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE
        v_hash bytea; i payment.idempotency_records;
        a payment.ledger_accounts; r payment.ledger_accounts; locked uuid;
        p payment.payments; journal uuid; code integer; response jsonb;
    BEGIN
        IF p_owner IS NULL OR p_wallet IS NULL OR p_recipient IS NULL
           OR length(p_recipient) NOT BETWEEN 1 AND 128 OR p_recipient !~ '^[a-zA-Z0-9_.:@-]+$'
           OR p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 1000000000000
           OR p_currency IS NULL OR p_currency NOT IN ('EUR','USD','JPY')
           OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 128 OR p_key !~ '^[!-~]+$' THEN
            RAISE EXCEPTION 'invalid payment parameters' USING ERRCODE='22023';
        END IF;
        v_hash := sha256(convert_to(jsonb_build_object('v',1,'wallet_id',p_wallet,
            'recipient',p_recipient,'amount_minor',p_amount,'currency',p_currency)::text,'UTF8'));
        INSERT INTO payment.idempotency_records(user_id,operation_kind,key,request_hash)
            VALUES(p_owner,'payment',p_key,v_hash)
            ON CONFLICT(user_id,operation_kind,key) DO NOTHING;
        SELECT * INTO STRICT i FROM payment.idempotency_records
            WHERE user_id=p_owner AND operation_kind='payment' AND key=p_key FOR UPDATE;
        IF i.request_hash<>v_hash THEN
            RETURN QUERY SELECT 409,jsonb_build_object('detail','Idempotency key conflict'); RETURN;
        END IF;
        IF i.response_status IS NOT NULL THEN
            RETURN QUERY SELECT i.response_status::integer,i.response_body; RETURN;
        END IF;
        SELECT la.* INTO a FROM payment.ledger_accounts la JOIN payment.wallets w ON
            w.id=la.wallet_id
            WHERE w.id=p_wallet AND w.user_id=p_owner AND la.account_kind='available';
        SELECT * INTO r FROM payment.ledger_accounts
            WHERE wallet_id=p_wallet AND account_kind='reserved';
        IF a.id IS NULL THEN
            code:=404; response:=jsonb_build_object('detail','Resource not found');
        ELSIF a.currency<>p_currency THEN
            code:=422; response:=jsonb_build_object('detail','Incompatible currency');
        ELSE
            FOR locked IN SELECT id FROM payment.ledger_accounts WHERE id IN (a.id,r.id) ORDER BY id
            LOOP PERFORM 1 FROM payment.ledger_accounts WHERE id=locked FOR UPDATE; END LOOP;
            SELECT * INTO STRICT a FROM payment.ledger_accounts WHERE id=a.id;
            SELECT * INTO STRICT r FROM payment.ledger_accounts WHERE id=r.id;
            IF a.balance_minor<p_amount THEN
                code:=409; response:=jsonb_build_object('detail','Insufficient funds');
            ELSIF r.balance_minor::numeric+p_amount>9223372036854775807 THEN
                code:=409; response:=jsonb_build_object('detail','Balance limit exceeded');
            ELSE
                INSERT INTO payment.payments(user_id,idempotency_id,wallet_id,recipient,
                                             amount_minor,currency)
                    VALUES(p_owner,i.id,p_wallet,p_recipient,p_amount,p_currency) RETURNING * INTO
                        p;
                INSERT INTO payment.ledger_journals(kind,payment_id,currency)
                    VALUES('reservation',p.id,p_currency) RETURNING id INTO journal;
                PERFORM payment.post_pair(journal,a.id,r.id,p_amount,p_currency);
                INSERT INTO payment.payment_outbox(payment_id) VALUES(p.id);
                INSERT INTO payment.audit_events(user_id,event_type,resource_id)
                    VALUES(p_owner,'payment_created',p.id);
                code:=202;
                response:=jsonb_build_object('id',p.id,'wallet_id',p_wallet,'recipient',p_recipient,
                    'amount_minor',p_amount,'currency',p_currency,'state','pending',
                    'created_at',p.created_at,'updated_at',p.updated_at);
            END IF;
        END IF;
        UPDATE payment.idempotency_records SET response_status=code,response_body=response WHERE
            id=i.id;
        RETURN QUERY SELECT code,response;
    END $$;

    CREATE FUNCTION payment.claim_payment(p_lease_seconds integer)
    RETURNS TABLE(payment_id uuid,generation bigint,attempts integer,state text,
                  recipient text,amount_minor bigint,currency text)
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE o payment.payment_outbox; p payment.payments;
    BEGIN
        IF p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 1 AND 300 THEN
            RAISE EXCEPTION 'invalid lease' USING ERRCODE='22023';
        END IF;
        -- Queue locks precede payment locks in BOTH claim and finish. Account locks come last.
        SELECT * INTO o FROM payment.payment_outbox q WHERE NOT q.done
            AND q.next_attempt_at<=clock_timestamp()
            AND (q.lease_until IS NULL OR q.lease_until<=clock_timestamp())
            ORDER BY q.next_attempt_at,q.payment_id FOR UPDATE SKIP LOCKED LIMIT 1;
        IF NOT FOUND THEN RETURN; END IF;
        SELECT * INTO STRICT p FROM payment.payments WHERE id=o.payment_id FOR UPDATE;
        IF p.state='pending' AND o.attempts>=5 THEN
            UPDATE payment.payments SET state='needs_review',updated_at=clock_timestamp()
                WHERE id=p.id RETURNING * INTO p;
            INSERT INTO payment.audit_events(user_id,event_type,resource_id)
                VALUES(p.user_id,'payment_needs_review',p.id);
        END IF;
        UPDATE payment.payment_outbox q SET generation=q.generation+1,
            attempts=CASE WHEN p.state='pending' THEN q.attempts+1 ELSE q.attempts END,
            lease_until=clock_timestamp()+make_interval(secs=>p_lease_seconds)
            WHERE q.payment_id=p.id RETURNING * INTO o;
        RETURN QUERY SELECT
            p.id,o.generation,o.attempts,p.state,p.recipient,p.amount_minor,p.currency;
    END $$;

    CREATE FUNCTION payment.finish_payment(p_id uuid,p_generation bigint,p_outcome text)
    RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    DECLARE o payment.payment_outbox; p payment.payments;
        debit uuid; credit uuid; journal uuid; phase text;
    BEGIN
        IF p_outcome IS NULL OR p_outcome NOT IN ('succeeded','failed','unknown') THEN
            RAISE EXCEPTION 'invalid provider outcome' USING ERRCODE='22023';
        END IF;
        SELECT * INTO o FROM payment.payment_outbox WHERE payment_id=p_id FOR UPDATE;
        IF NOT FOUND OR o.done OR p_generation IS NULL OR o.generation<>p_generation
           OR o.lease_until IS NULL OR o.lease_until<=clock_timestamp() THEN RETURN false; END IF;
        SELECT * INTO STRICT p FROM payment.payments WHERE id=p_id FOR UPDATE;
        IF p.state IN ('succeeded','failed') THEN RETURN false; END IF;
        IF p_outcome='unknown' THEN
            IF p.state='pending' AND o.attempts>=5 THEN
                UPDATE payment.payments SET state='needs_review',updated_at=clock_timestamp()
                    WHERE id=p.id;
                INSERT INTO payment.audit_events(user_id,event_type,resource_id)
                    VALUES(p.user_id,'payment_needs_review',p.id);
            END IF;
            UPDATE payment.payment_outbox SET lease_until=NULL,
                next_attempt_at=clock_timestamp()+make_interval(secs=>CASE
                    WHEN o.attempts>=5 THEN 60
                    ELSE least(60,power(2,o.attempts))*(0.5+random()) END)
                WHERE payment_id=p.id;
            RETURN true;
        END IF;
        SELECT id INTO STRICT debit FROM payment.ledger_accounts
            WHERE wallet_id=p.wallet_id AND account_kind='reserved';
        IF p_outcome='succeeded' THEN
            phase:='settlement';
            SELECT id INTO STRICT credit FROM payment.ledger_accounts
                WHERE wallet_id IS NULL AND account_kind='clearing' AND currency=p.currency;
        ELSE
            phase:='release';
            SELECT id INTO STRICT credit FROM payment.ledger_accounts
                WHERE wallet_id=p.wallet_id AND account_kind='available';
        END IF;
        INSERT INTO payment.ledger_journals(kind,payment_id,currency)
            VALUES(phase,p.id,p.currency) RETURNING id INTO journal;
        PERFORM payment.post_pair(journal,debit,credit,p.amount_minor,p.currency);
        UPDATE payment.payments SET state=p_outcome,updated_at=clock_timestamp() WHERE id=p.id;
        UPDATE payment.payment_outbox SET done=true,lease_until=NULL WHERE payment_id=p.id;
        INSERT INTO payment.audit_events(user_id,event_type,resource_id)
            VALUES(p.user_id,'payment_'||p_outcome,p.id);
        RETURN true;
    END $$;
    REVOKE ALL ON FUNCTION payment.create_payment(uuid,text,uuid,text,bigint,text),
        payment.claim_payment(integer),payment.finish_payment(uuid,bigint,text)
        FROM PUBLIC,payment_app,payment_worker;
    GRANT EXECUTE ON FUNCTION payment.create_payment(uuid,text,uuid,text,bigint,text) TO
        payment_app;
    GRANT EXECUTE ON FUNCTION
        payment.claim_payment(integer),payment.finish_payment(uuid,bigint,text)
        TO payment_worker;
"""
