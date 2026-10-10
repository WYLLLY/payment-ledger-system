"""Identity, opaque sessions, auth counters and atomic empty wallet creation."""

from alembic import op

revision: str = "0002_identity_wallets"
down_revision: str | None = "0001_foundation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE payment.users (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email text NOT NULL CONSTRAINT uq_users_email UNIQUE,
            password_hash text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            CONSTRAINT ck_users_email CHECK (
                email = lower(email) AND length(email) BETWEEN 3 AND 254
            ),
            CONSTRAINT ck_users_password_hash CHECK (password_hash LIKE '$argon2id$%')
        );
        CREATE TABLE payment.sessions (
            token_hash bytea PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES payment.users(id),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            CONSTRAINT ck_sessions_token_hash CHECK (octet_length(token_hash) = 32),
            CONSTRAINT ck_sessions_expiry CHECK (expires_at > created_at),
            CONSTRAINT ck_sessions_revocation CHECK (revoked_at >= created_at)
        );
        CREATE INDEX ix_sessions_user_id ON payment.sessions(user_id);
        CREATE TABLE payment.wallets (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES payment.users(id),
            currency text NOT NULL CHECK (currency IN ('EUR', 'USD', 'JPY')),
            created_at timestamptz NOT NULL DEFAULT statement_timestamp(),
            CONSTRAINT uq_wallets_user_currency UNIQUE (user_id, currency),
            CONSTRAINT uq_wallets_id_currency UNIQUE (id, currency)
        );
        CREATE TABLE payment.ledger_accounts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            wallet_id uuid NOT NULL,
            currency text NOT NULL,
            account_kind text NOT NULL CHECK (account_kind IN ('available', 'reserved')),
            balance_minor bigint NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
            CONSTRAINT fk_accounts_wallet_currency FOREIGN KEY (wallet_id, currency)
                REFERENCES payment.wallets(id, currency),
            CONSTRAINT uq_accounts_wallet_kind UNIQUE (wallet_id, account_kind)
        );
        CREATE TABLE payment.audit_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid REFERENCES payment.users(id),
            event_type text NOT NULL CHECK (event_type IN (
                'user_registered', 'login_succeeded', 'login_failed',
                'session_revoked', 'wallet_created'
            )),
            resource_id uuid,
            occurred_at timestamptz NOT NULL DEFAULT statement_timestamp()
        );
        CREATE TABLE payment.auth_rate_limits (
            key_hash bytea PRIMARY KEY CHECK (octet_length(key_hash) = 32),
            window_start bigint NOT NULL,
            attempts integer NOT NULL CHECK (attempts BETWEEN 1 AND 1001)
        );
    """)
    op.execute("""
        CREATE FUNCTION payment.assert_wallet_pair(p_wallet_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF EXISTS (SELECT FROM payment.wallets WHERE id = p_wallet_id)
               AND (SELECT count(*) FROM payment.ledger_accounts
                    WHERE wallet_id = p_wallet_id) <> 2 THEN
                RAISE EXCEPTION 'wallet requires available and reserved accounts'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_wallet_account_pair';
            END IF;
        END $$;

        CREATE FUNCTION payment.check_wallet_pair() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_TABLE_NAME = 'wallets' THEN
                PERFORM payment.assert_wallet_pair(NEW.id);
            ELSE
                IF TG_OP <> 'INSERT' THEN
                    PERFORM payment.assert_wallet_pair(OLD.wallet_id);
                END IF;
                IF TG_OP <> 'DELETE' THEN
                    PERFORM payment.assert_wallet_pair(NEW.wallet_id);
                END IF;
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER wallets_require_pair
            AFTER INSERT OR UPDATE ON payment.wallets
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION payment.check_wallet_pair();
        CREATE CONSTRAINT TRIGGER accounts_require_pair
            AFTER INSERT OR UPDATE OR DELETE ON payment.ledger_accounts
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION payment.check_wallet_pair();

        CREATE FUNCTION payment.create_wallet(p_user_id uuid, p_currency text) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_wallet_id uuid;
        BEGIN
            IF p_user_id IS NULL OR NOT EXISTS (
                SELECT FROM payment.users WHERE id = p_user_id
            ) THEN
                RAISE EXCEPTION 'unknown owner' USING ERRCODE = '23503';
            END IF;
            INSERT INTO payment.wallets(user_id, currency) VALUES (p_user_id, p_currency)
                RETURNING id INTO v_wallet_id;
            INSERT INTO payment.ledger_accounts(wallet_id, currency, account_kind)
                VALUES (v_wallet_id, p_currency, 'available'),
                       (v_wallet_id, p_currency, 'reserved');
            INSERT INTO payment.audit_events(user_id, event_type, resource_id)
                VALUES (p_user_id, 'wallet_created', v_wallet_id);
            RETURN v_wallet_id;
        END $$;

        CREATE FUNCTION payment.revoke_session(p_token_hash bytea) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_user_id uuid;
        BEGIN
            UPDATE payment.sessions SET revoked_at = statement_timestamp()
                WHERE token_hash = p_token_hash AND revoked_at IS NULL
                  AND expires_at > statement_timestamp()
                RETURNING user_id INTO v_user_id;
            IF v_user_id IS NOT NULL THEN
                INSERT INTO payment.audit_events(user_id, event_type)
                    VALUES (v_user_id, 'session_revoked');
            END IF;
            RETURN v_user_id;
        END $$;

        CREATE FUNCTION payment.take_auth_slot(
            p_key_hash bytea, p_limit integer, p_window_seconds integer
        ) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
        DECLARE v_start bigint; v_count integer; v_current bigint;
        BEGIN
            IF p_key_hash IS NULL OR octet_length(p_key_hash) <> 32
               OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 1000
               OR p_window_seconds IS NULL OR p_window_seconds NOT BETWEEN 1 AND 3600 THEN
                RAISE EXCEPTION 'invalid rate limit parameters' USING ERRCODE = '22023';
            END IF;
            v_start := floor(extract(epoch FROM clock_timestamp()) / p_window_seconds)::bigint
                       * p_window_seconds;
            INSERT INTO payment.auth_rate_limits AS counters(key_hash, window_start, attempts)
                VALUES (p_key_hash, v_start, 1)
                ON CONFLICT (key_hash) DO UPDATE SET
                    attempts = CASE WHEN counters.window_start < EXCLUDED.window_start THEN 1
                                    ELSE LEAST(counters.attempts + 1, 1001) END,
                    window_start = GREATEST(counters.window_start, EXCLUDED.window_start)
                RETURNING window_start, attempts INTO v_current, v_count;
            IF v_count > p_limit THEN
                RETURN GREATEST(1, ceil(v_current + p_window_seconds
                                     - extract(epoch FROM clock_timestamp()))::integer);
            END IF;
            RETURN 0;
        END $$;
    """)
    op.execute("""
        GRANT SELECT ON payment.users, payment.sessions, payment.wallets,
            payment.ledger_accounts TO payment_app;
        GRANT INSERT (email, password_hash) ON payment.users TO payment_app;
        GRANT INSERT (token_hash, user_id, expires_at) ON payment.sessions TO payment_app;
        GRANT INSERT (user_id, event_type, resource_id) ON payment.audit_events TO payment_app;
        REVOKE ALL ON FUNCTION payment.assert_wallet_pair(uuid), payment.check_wallet_pair(),
            payment.create_wallet(uuid, text), payment.revoke_session(bytea),
            payment.take_auth_slot(bytea, integer, integer) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION payment.create_wallet(uuid, text),
            payment.revoke_session(bytea), payment.take_auth_slot(bytea, integer, integer)
            TO payment_app;
    """)


def downgrade() -> None:
    op.execute("""
        DROP FUNCTION payment.take_auth_slot(bytea, integer, integer);
        DROP FUNCTION payment.revoke_session(bytea);
        DROP FUNCTION payment.create_wallet(uuid, text);
        DROP TRIGGER accounts_require_pair ON payment.ledger_accounts;
        DROP TRIGGER wallets_require_pair ON payment.wallets;
        DROP FUNCTION payment.check_wallet_pair();
        DROP FUNCTION payment.assert_wallet_pair(uuid);
        DROP TABLE payment.auth_rate_limits;
        DROP TABLE payment.audit_events;
        DROP TABLE payment.ledger_accounts;
        DROP TABLE payment.wallets;
        DROP TABLE payment.sessions;
        DROP TABLE payment.users;
    """)
