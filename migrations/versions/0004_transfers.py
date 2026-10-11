"""Atomic transfers, retained idempotency responses and shared pair posting."""

from alembic import op

revision: str = "0004_transfers"
down_revision: str | None = "0003_ledger"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE payment.idempotency_records (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES payment.users(id),
            operation_kind text NOT NULL CHECK (operation_kind = 'transfer'),
            key text NOT NULL CHECK (length(key) BETWEEN 1 AND 128 AND key ~ '^[!-~]+$'),
            request_hash bytea NOT NULL CHECK (octet_length(request_hash) = 32),
            response_status smallint CHECK (response_status IN (201, 404, 409, 422)),
            response_body jsonb CHECK (jsonb_typeof(response_body) = 'object'),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            UNIQUE (user_id, operation_kind, key),
            CHECK ((response_status IS NULL) = (response_body IS NULL))
        );
        CREATE TABLE payment.transfers (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES payment.users(id),
            idempotency_id uuid NOT NULL UNIQUE REFERENCES payment.idempotency_records(id),
            source_wallet_id uuid NOT NULL,
            destination_wallet_id uuid NOT NULL,
            currency text NOT NULL CHECK (currency IN ('EUR', 'USD', 'JPY')),
            amount_minor bigint NOT NULL CHECK (amount_minor BETWEEN 1 AND 1000000000000),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            CHECK (source_wallet_id <> destination_wallet_id),
            FOREIGN KEY (source_wallet_id, currency) REFERENCES payment.wallets(id, currency),
            FOREIGN KEY (destination_wallet_id, currency) REFERENCES payment.wallets(id, currency),
            UNIQUE (id, currency)
        );
        CREATE INDEX ix_transfers_owner_created ON payment.transfers(user_id, created_at, id);
        ALTER TABLE payment.ledger_journals
            ALTER COLUMN opening_wallet_id DROP NOT NULL,
            DROP CONSTRAINT ledger_journals_kind_check,
            ADD COLUMN transfer_id uuid UNIQUE,
            ADD FOREIGN KEY (transfer_id, currency) REFERENCES payment.transfers(id, currency),
            ADD CONSTRAINT ck_journal_operation CHECK (
                (kind='opening' AND opening_wallet_id IS NOT NULL AND transfer_id IS NULL) OR
                (kind='transfer' AND opening_wallet_id IS NULL AND transfer_id IS NOT NULL)
            );
        ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN ('user_registered', 'login_succeeded', 'login_failed',
                          'session_revoked', 'wallet_created', 'ledger_opened', 'transfer_created')
        );

        CREATE FUNCTION payment.post_pair(
            p_journal uuid, p_debit uuid, p_credit uuid, p_amount bigint, p_currency text
        ) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_account uuid;
        BEGIN
            IF p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 1000000000000
               OR p_debit IS NULL OR p_credit IS NULL OR p_debit = p_credit THEN
                RAISE EXCEPTION 'invalid pair' USING ERRCODE='22023';
            END IF;
            FOR v_account IN SELECT id FROM payment.ledger_accounts
                WHERE id IN (p_debit, p_credit) ORDER BY id
            LOOP
                PERFORM 1 FROM payment.ledger_accounts WHERE id=v_account FOR UPDATE;
            END LOOP;
            INSERT INTO payment.ledger_entries(journal_id, account_id, currency, amount_minor)
                VALUES (p_journal, p_debit, p_currency, -p_amount),
                       (p_journal, p_credit, p_currency, p_amount);
        END $$;

        CREATE FUNCTION payment.protect_idempotency() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RAISE EXCEPTION 'financial idempotency is retained' USING ERRCODE='23514';
            END IF;
            IF OLD.response_status IS NOT NULL
               OR (to_jsonb(OLD) - 'response_status' - 'response_body') IS DISTINCT FROM
                  (to_jsonb(NEW) - 'response_status' - 'response_body') THEN
                RAISE EXCEPTION 'financial idempotency is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER immutable_idempotency BEFORE UPDATE OR DELETE ON payment.idempotency_records
            FOR EACH ROW EXECUTE FUNCTION payment.protect_idempotency();
        CREATE TRIGGER retained_idempotency BEFORE TRUNCATE ON payment.idempotency_records
            FOR EACH STATEMENT EXECUTE FUNCTION payment.protect_idempotency();
        CREATE TRIGGER immutable_transfers BEFORE UPDATE OR DELETE ON payment.transfers
            FOR EACH ROW EXECUTE FUNCTION payment.reject_ledger_mutation();
        CREATE TRIGGER retained_transfers BEFORE TRUNCATE ON payment.transfers
            FOR EACH STATEMENT EXECUTE FUNCTION payment.reject_ledger_mutation();

        CREATE FUNCTION payment.check_idempotency_complete() RETURNS trigger
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
        CREATE CONSTRAINT TRIGGER idempotency_complete AFTER INSERT OR UPDATE
            ON payment.idempotency_records DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION payment.check_idempotency_complete();

        CREATE FUNCTION payment.check_transfer_complete() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT FROM payment.transfers t
                JOIN payment.wallets w ON w.id=t.source_wallet_id AND w.user_id=t.user_id
                JOIN payment.idempotency_records i ON i.id=t.idempotency_id AND i.user_id=t.user_id
                    AND i.response_status=201 AND i.response_body->>'id'=t.id::text
                JOIN payment.ledger_journals j ON j.transfer_id=t.id AND j.kind='transfer'
                JOIN payment.ledger_accounts s ON s.wallet_id=t.source_wallet_id
                    AND s.account_kind='available'
                JOIN payment.ledger_accounts d ON d.wallet_id=t.destination_wallet_id
                    AND d.account_kind='available'
                JOIN payment.ledger_entries debit ON debit.journal_id=j.id
                    AND debit.account_id=s.id AND debit.amount_minor=-t.amount_minor
                JOIN payment.ledger_entries credit ON credit.journal_id=j.id
                    AND credit.account_id=d.id AND credit.amount_minor=t.amount_minor
                WHERE t.id=NEW.id
            ) THEN
                RAISE EXCEPTION 'incomplete or inconsistent transfer' USING ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER transfer_complete AFTER INSERT ON payment.transfers
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION payment.check_transfer_complete();

        CREATE FUNCTION payment.create_transfer(
            p_owner uuid, p_key text, p_source uuid, p_destination uuid,
            p_amount bigint, p_currency text
        ) RETURNS TABLE(status_code integer, body jsonb)
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            v_hash bytea; v_record payment.idempotency_records;
            v_source payment.ledger_accounts; v_destination payment.ledger_accounts;
            v_account uuid; v_transfer uuid; v_journal uuid; v_created timestamptz;
            v_status integer; v_body jsonb;
        BEGIN
            IF p_owner IS NULL OR p_source IS NULL OR p_destination IS NULL
               OR p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 1000000000000
               OR p_currency IS NULL OR p_currency NOT IN ('EUR','USD','JPY')
               OR p_key IS NULL OR length(p_key) NOT BETWEEN 1 AND 128 OR p_key !~ '^[!-~]+$' THEN
                RAISE EXCEPTION 'invalid transfer parameters' USING ERRCODE='22023';
            END IF;
            -- Typed UUIDs/BIGINT plus jsonb's canonical key order define payload v1.
            -- Hash in the DB: callers cannot supply a hash unrelated to their parameters.
            v_hash := sha256(convert_to(jsonb_build_object(
                'v',1,'source_wallet_id',p_source,'destination_wallet_id',p_destination,
                'amount_minor',p_amount,'currency',p_currency
            )::text,'UTF8'));
            INSERT INTO payment.idempotency_records(user_id,operation_kind,key,request_hash)
                VALUES (p_owner,'transfer',p_key,v_hash)
                ON CONFLICT (user_id,operation_kind,key) DO NOTHING;
            -- INSERT waits for concurrent unique contenders; this next READ COMMITTED
            -- statement sees the winner's committed response (or claims after rollback).
            SELECT * INTO STRICT v_record FROM payment.idempotency_records
                WHERE user_id=p_owner AND operation_kind='transfer' AND key=p_key FOR UPDATE;
            IF v_record.request_hash <> v_hash THEN
                RETURN QUERY SELECT 409, jsonb_build_object('detail','Idempotency key conflict');
                RETURN;
            END IF;
            IF v_record.response_status IS NOT NULL THEN
                RETURN QUERY SELECT v_record.response_status::integer, v_record.response_body;
                RETURN;
            END IF;

            SELECT a.* INTO v_source FROM payment.ledger_accounts a
                JOIN payment.wallets w ON w.id=a.wallet_id
                WHERE w.id=p_source AND w.user_id=p_owner AND a.account_kind='available';
            SELECT a.* INTO v_destination FROM payment.ledger_accounts a
                WHERE a.wallet_id=p_destination AND a.account_kind='available';
            IF v_source.id IS NULL OR v_destination.id IS NULL THEN
                v_status := 404; v_body := jsonb_build_object('detail','Resource not found');
            ELSIF p_source = p_destination THEN
                v_status := 422; v_body := jsonb_build_object('detail','Distinct wallets required');
            ELSIF v_source.currency <> p_currency OR v_destination.currency <> p_currency THEN
                v_status := 422; v_body := jsonb_build_object('detail','Incompatible currency');
            ELSE
                FOR v_account IN SELECT id FROM payment.ledger_accounts
                    WHERE id IN (v_source.id, v_destination.id) ORDER BY id
                LOOP
                    PERFORM 1 FROM payment.ledger_accounts WHERE id=v_account FOR UPDATE;
                END LOOP;
                -- Re-read after waiting for locks; pre-lock balances may be stale.
                SELECT * INTO STRICT v_source FROM payment.ledger_accounts WHERE id=v_source.id;
                SELECT * INTO STRICT v_destination FROM payment.ledger_accounts
                    WHERE id=v_destination.id;
                IF v_source.balance_minor < p_amount THEN
                    v_status := 409; v_body := jsonb_build_object('detail','Insufficient funds');
                ELSIF v_destination.balance_minor::numeric + p_amount > 9223372036854775807 THEN
                    v_status := 409;
                    v_body := jsonb_build_object('detail','Balance limit exceeded');
                ELSE
                    INSERT INTO payment.transfers(user_id,idempotency_id,source_wallet_id,
                                                  destination_wallet_id,currency,amount_minor)
                        VALUES (p_owner,v_record.id,p_source,p_destination,p_currency,p_amount)
                        RETURNING id,created_at INTO v_transfer,v_created;
                    INSERT INTO payment.ledger_journals(kind,transfer_id,currency)
                        VALUES ('transfer',v_transfer,p_currency) RETURNING id INTO v_journal;
                    PERFORM payment.post_pair(
                        v_journal,v_source.id,v_destination.id,p_amount,p_currency
                    );
                    INSERT INTO payment.audit_events(user_id,event_type,resource_id)
                        VALUES (p_owner,'transfer_created',v_transfer);
                    v_status := 201;
                    v_body := jsonb_build_object(
                        'id',v_transfer,'source_wallet_id',p_source,
                        'destination_wallet_id',p_destination,'currency',p_currency,
                        'amount_minor',p_amount,'created_at',v_created,'journal_id',v_journal
                    );
                END IF;
            END IF;
            UPDATE payment.idempotency_records SET response_status=v_status,response_body=v_body
                WHERE id=v_record.id;
            RETURN QUERY SELECT v_status,v_body;
        END $$;
        REVOKE ALL ON FUNCTION payment.post_pair(uuid,uuid,uuid,bigint,text),
            payment.protect_idempotency(), payment.check_idempotency_complete(),
            payment.check_transfer_complete(),
            payment.create_transfer(uuid,text,uuid,uuid,bigint,text)
            FROM PUBLIC,payment_app,payment_worker;
        GRANT EXECUTE ON FUNCTION payment.create_transfer(uuid,text,uuid,uuid,bigint,text)
            TO payment_app;
        GRANT SELECT ON payment.transfers TO payment_app;
    """)
    op.execute(
        OPENING_FUNCTION.replace(
            OPENING_PAIR_INSERT,
            """
            PERFORM payment.post_pair(v_id, p_debit, p_credit, p_amount, p_currency);
    """,
        )
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM payment.idempotency_records)
               OR EXISTS (SELECT FROM payment.transfers) THEN
                RAISE EXCEPTION 'cannot downgrade retained transfer or idempotency history';
            END IF;
        END $$;
        DROP FUNCTION payment.create_transfer(uuid,text,uuid,uuid,bigint,text);
        DROP TRIGGER transfer_complete ON payment.transfers;
        DROP FUNCTION payment.check_transfer_complete();
        DROP TRIGGER idempotency_complete ON payment.idempotency_records;
        DROP FUNCTION payment.check_idempotency_complete();
        ALTER TABLE payment.ledger_journals DROP CONSTRAINT ck_journal_operation,
            DROP COLUMN transfer_id, ALTER COLUMN opening_wallet_id SET NOT NULL,
            ADD CONSTRAINT ledger_journals_kind_check CHECK (kind='opening');
        DROP TABLE payment.transfers;
        DROP TABLE payment.idempotency_records;
        DROP FUNCTION payment.protect_idempotency();
        ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN ('user_registered','login_succeeded','login_failed',
                          'session_revoked','wallet_created','ledger_opened')
        );
    """)
    op.execute(OPENING_FUNCTION)
    op.execute("DROP FUNCTION payment.post_pair(uuid,uuid,uuid,bigint,text)")


# Restore the exact M3 posting contract on supported empty-M4 downgrades.
OPENING_PAIR_INSERT = """
            INSERT INTO payment.ledger_entries(journal_id, account_id, currency, amount_minor)
                VALUES (v_id, p_debit, p_currency, -p_amount),
                       (v_id, p_credit, p_currency, p_amount);""".lstrip("\n")

OPENING_FUNCTION = """CREATE OR REPLACE FUNCTION payment.post_opening_pair(
            p_owner uuid, p_wallet uuid, p_debit uuid, p_credit uuid,
            p_amount bigint, p_currency text
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_id uuid; v_account uuid;
        BEGIN
            IF p_amount IS NULL OR p_amount NOT BETWEEN 1 AND 1000000000000
               OR p_currency IS NULL OR p_currency NOT IN ('EUR', 'USD', 'JPY')
               OR p_debit IS NULL OR p_credit IS NULL OR p_debit = p_credit THEN
                RAISE EXCEPTION 'invalid posting parameters' USING ERRCODE = '22023';
            END IF;
            -- Every monetary writer locks both accounts individually in UUID order.
            FOR v_account IN SELECT id FROM payment.ledger_accounts
                WHERE id IN (p_debit, p_credit) ORDER BY id
            LOOP
                PERFORM 1 FROM payment.ledger_accounts WHERE id = v_account FOR UPDATE;
            END LOOP;
            IF NOT EXISTS (
                SELECT FROM payment.ledger_accounts a JOIN payment.wallets w ON w.id = a.wallet_id
                WHERE a.id = p_credit AND a.wallet_id = p_wallet AND w.user_id = p_owner
                  AND a.currency = p_currency AND w.currency = p_currency
                  AND a.account_kind = 'available'
            ) OR NOT EXISTS (
                SELECT FROM payment.ledger_accounts WHERE id = p_debit
                  AND wallet_id IS NULL AND account_kind = 'funding' AND currency = p_currency
            ) THEN
                RAISE EXCEPTION 'invalid opening accounts or owner' USING ERRCODE = '22023';
            END IF;
            INSERT INTO payment.ledger_journals(kind, opening_wallet_id, currency)
                VALUES ('opening', p_wallet, p_currency) RETURNING id INTO v_id;
            INSERT INTO payment.ledger_entries(journal_id, account_id, currency, amount_minor)
                VALUES (v_id, p_debit, p_currency, -p_amount),
                       (v_id, p_credit, p_currency, p_amount);
            INSERT INTO payment.audit_events(user_id, event_type, resource_id)
                VALUES (p_owner, 'ledger_opened', v_id);
            RETURN v_id;
        END $$;"""
