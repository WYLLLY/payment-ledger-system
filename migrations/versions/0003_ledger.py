"""Immutable double-entry ledger and private, fixed dev/test openings.

No dev/test role is provisioned by migrations or the demonstration deployment.
"""

from alembic import op

revision: str = "0003_ledger"
down_revision: str | None = "0002_identity_wallets"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE payment.ledger_accounts
            ALTER COLUMN wallet_id DROP NOT NULL,
            DROP CONSTRAINT ledger_accounts_account_kind_check,
            DROP CONSTRAINT ledger_accounts_balance_minor_check,
            ADD CONSTRAINT ck_account_kind_owner CHECK (
                (wallet_id IS NOT NULL AND account_kind IN ('available', 'reserved')) OR
                (wallet_id IS NULL AND account_kind IN ('funding', 'clearing'))
            ),
            ADD CONSTRAINT ck_account_currency CHECK (currency IN ('EUR', 'USD', 'JPY')),
            ADD CONSTRAINT ck_user_balance CHECK (wallet_id IS NULL OR balance_minor >= 0),
            ADD CONSTRAINT uq_account_id_currency UNIQUE (id, currency);
        CREATE UNIQUE INDEX uq_system_account_currency_kind
            ON payment.ledger_accounts(currency, account_kind) WHERE wallet_id IS NULL;
        INSERT INTO payment.ledger_accounts(currency, account_kind)
            SELECT currency, kind FROM unnest(ARRAY['EUR', 'USD', 'JPY']) AS currency
            CROSS JOIN unnest(ARRAY['funding', 'clearing']) AS kind;

        CREATE TABLE payment.ledger_journals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind text NOT NULL CHECK (kind = 'opening'),
            opening_wallet_id uuid NOT NULL UNIQUE,
            currency text NOT NULL CHECK (currency IN ('EUR', 'USD', 'JPY')),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            CONSTRAINT uq_journal_id_currency UNIQUE (id, currency),
            FOREIGN KEY (opening_wallet_id, currency) REFERENCES payment.wallets(id, currency)
        );
        CREATE TABLE payment.ledger_entries (
            journal_id uuid NOT NULL,
            account_id uuid NOT NULL,
            currency text NOT NULL,
            amount_minor bigint NOT NULL CHECK (
                amount_minor BETWEEN -1000000000000 AND 1000000000000 AND amount_minor <> 0
            ),
            PRIMARY KEY (journal_id, account_id),
            FOREIGN KEY (journal_id, currency) REFERENCES payment.ledger_journals(id, currency),
            FOREIGN KEY (account_id, currency) REFERENCES payment.ledger_accounts(id, currency)
        );
        CREATE INDEX ix_entries_account ON payment.ledger_entries(account_id, journal_id);

        CREATE FUNCTION payment.reject_ledger_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            RAISE EXCEPTION 'ledger history is immutable' USING ERRCODE = '23514';
        END $$;
        CREATE TRIGGER immutable_journals BEFORE UPDATE OR DELETE ON payment.ledger_journals
            FOR EACH ROW EXECUTE FUNCTION payment.reject_ledger_mutation();
        CREATE TRIGGER immutable_entries BEFORE UPDATE OR DELETE ON payment.ledger_entries
            FOR EACH ROW EXECUTE FUNCTION payment.reject_ledger_mutation();
        CREATE TRIGGER no_truncate_journals BEFORE TRUNCATE ON payment.ledger_journals
            FOR EACH STATEMENT EXECUTE FUNCTION payment.reject_ledger_mutation();
        CREATE TRIGGER no_truncate_entries BEFORE TRUNCATE ON payment.ledger_entries
            FOR EACH STATEMENT EXECUTE FUNCTION payment.reject_ledger_mutation();

        CREATE FUNCTION payment.apply_ledger_entry() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            UPDATE payment.ledger_accounts SET balance_minor = balance_minor + NEW.amount_minor
            WHERE id = NEW.account_id AND currency = NEW.currency
              AND (wallet_id IS NULL OR balance_minor + NEW.amount_minor >= 0);
            IF NOT FOUND THEN
                RAISE EXCEPTION 'account missing or insufficient funds' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END $$;
        CREATE TRIGGER project_ledger_entry AFTER INSERT ON payment.ledger_entries
            FOR EACH ROW EXECUTE FUNCTION payment.apply_ledger_entry();

        CREATE FUNCTION payment.check_journal_pair() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_id uuid; v_count bigint; v_sum numeric;
        BEGIN
            IF TG_TABLE_NAME = 'ledger_journals' THEN v_id := NEW.id;
            ELSE v_id := NEW.journal_id; END IF;
            SELECT count(*), sum(amount_minor) INTO v_count, v_sum
                FROM payment.ledger_entries WHERE journal_id = v_id;
            IF v_count <> 2 OR v_sum <> 0 THEN
                RAISE EXCEPTION 'journal requires exactly two opposite entries'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_journal_pair';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER journal_requires_pair
            AFTER INSERT ON payment.ledger_journals DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION payment.check_journal_pair();
        CREATE CONSTRAINT TRIGGER entries_require_pair
            AFTER INSERT ON payment.ledger_entries DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION payment.check_journal_pair();

        CREATE FUNCTION payment.post_opening_pair(
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
        END $$;

        CREATE FUNCTION payment.open_fixture_wallet(p_owner uuid, p_wallet uuid) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_currency text; v_credit uuid; v_debit uuid; v_amount bigint;
        BEGIN
            SELECT w.currency, a.id INTO v_currency, v_credit FROM payment.wallets w
                JOIN payment.ledger_accounts a ON a.wallet_id = w.id
                WHERE w.id = p_wallet AND w.user_id = p_owner AND a.account_kind = 'available';
            IF NOT FOUND THEN
                RAISE EXCEPTION 'invalid fixture wallet or owner' USING ERRCODE = '22023';
            END IF;
            SELECT id INTO v_debit FROM payment.ledger_accounts
                WHERE wallet_id IS NULL AND account_kind = 'funding' AND currency = v_currency;
            -- Fixed fixture values in minor units; callers cannot select an arbitrary amount.
            v_amount := CASE v_currency WHEN 'JPY' THEN 10000 ELSE 100000 END;
            RETURN payment.post_opening_pair(
                p_owner, p_wallet, v_debit, v_credit, v_amount, v_currency
            );
        END $$;
        ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN ('user_registered', 'login_succeeded', 'login_failed',
                          'session_revoked', 'wallet_created', 'ledger_opened')
        );
        REVOKE ALL ON FUNCTION payment.reject_ledger_mutation(), payment.apply_ledger_entry(),
            payment.check_journal_pair(),
            payment.post_opening_pair(uuid, uuid, uuid, uuid, bigint, text),
            payment.open_fixture_wallet(uuid, uuid) FROM PUBLIC, payment_app, payment_worker;
        GRANT SELECT ON payment.ledger_journals, payment.ledger_entries TO payment_app;
    """)


def downgrade() -> None:
    # Never silently erase posted history to make a rollback appear successful.
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT FROM payment.ledger_journals) THEN
                RAISE EXCEPTION 'cannot downgrade a populated ledger';
            END IF;
        END $$;
        DROP FUNCTION payment.open_fixture_wallet(uuid, uuid);
        DROP FUNCTION payment.post_opening_pair(uuid, uuid, uuid, uuid, bigint, text);
        DROP TABLE payment.ledger_entries;
        DROP TABLE payment.ledger_journals;
        DROP FUNCTION payment.check_journal_pair();
        DROP FUNCTION payment.apply_ledger_entry();
        DROP FUNCTION payment.reject_ledger_mutation();
        DELETE FROM payment.ledger_accounts WHERE wallet_id IS NULL;
        SET CONSTRAINTS ALL IMMEDIATE;
        DROP INDEX payment.uq_system_account_currency_kind;
        ALTER TABLE payment.ledger_accounts
            DROP CONSTRAINT uq_account_id_currency,
            DROP CONSTRAINT ck_user_balance,
            DROP CONSTRAINT ck_account_currency,
            DROP CONSTRAINT ck_account_kind_owner,
            ALTER COLUMN wallet_id SET NOT NULL,
            ADD CONSTRAINT ledger_accounts_balance_minor_check CHECK (balance_minor >= 0),
            ADD CONSTRAINT ledger_accounts_account_kind_check
                CHECK (account_kind IN ('available', 'reserved'));
        ALTER TABLE payment.audit_events DROP CONSTRAINT audit_events_event_type_check;
        ALTER TABLE payment.audit_events ADD CONSTRAINT audit_events_event_type_check CHECK (
            event_type IN ('user_registered', 'login_succeeded', 'login_failed',
                          'session_revoked', 'wallet_created')
        );
    """)
