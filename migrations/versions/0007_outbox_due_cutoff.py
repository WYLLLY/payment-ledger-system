"""Use an indexable cutoff for outbox due-time eligibility."""

from alembic import op

revision: str = "0007_outbox_due_cutoff"
down_revision: str | None = "0006_resilience"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(CLAIM.format(due_cutoff="statement_timestamp()"))


def downgrade() -> None:
    op.execute(CLAIM.format(due_cutoff="clock_timestamp()"))


# Replacing the body preserves the function owner, signature and existing EXECUTE grants.
# The stable statement timestamp bounds the due index. Lease checks and the lease
# deadline still use the live clock.
CLAIM = """
    CREATE OR REPLACE FUNCTION payment.claim_payment(p_lease_seconds integer)
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
            AND q.next_attempt_at<={due_cutoff}
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
"""
