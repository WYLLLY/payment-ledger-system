"""Snapshot diagnostics and audited scheduling; only the worker finalizes payments."""

from typing import Literal, Protocol, TypedDict
from uuid import UUID

from sqlalchemy import Engine, text

from payment_ledger.ledger.reconciliation import RECONCILIATION_SQL
from payment_ledger.payments.provider import Outcome, ProviderClient, ProviderRequest
from payment_ledger.payments.service import transaction


class Issue(TypedDict):
    issue: str
    reference: str


class Metrics(TypedDict):
    pending: int
    needs_review: int
    oldest_outbox_age_seconds: int
    expired_leases: int


class Snapshot(TypedDict):
    issues: list[Issue]
    metrics: Metrics


class ExternalReport(TypedDict):
    findings: list[Issue]
    checked: int
    complete: bool
    next_cursor: str | None


class LookupProvider(Protocol):
    def lookup(self, payment: ProviderRequest) -> Outcome: ...


# One SQL statement gives internal checks and metrics a consistent READ COMMITTED snapshot.
SNAPSHOT_SQL = (
    """
WITH ledger_issues AS (
"""
    + RECONCILIATION_SQL
    + """
), expected_phases AS (
    SELECT p.*, 'reservation' AS phase FROM payment.payments p
    UNION ALL
    SELECT p.*, CASE state WHEN 'succeeded' THEN 'settlement' ELSE 'release' END
        FROM payment.payments p WHERE state IN ('succeeded','failed')
), payment_issues AS (
    SELECT 'payment_journal_mismatch' AS issue, p.id::text AS reference
    FROM expected_phases p WHERE NOT EXISTS (
        SELECT FROM payment.ledger_journals j
        JOIN payment.ledger_entries d ON d.journal_id=j.id AND d.amount_minor=-p.amount_minor
        JOIN payment.ledger_entries c ON c.journal_id=j.id AND c.amount_minor=p.amount_minor
        JOIN payment.ledger_accounts da ON da.id=d.account_id AND da.wallet_id=p.wallet_id
            AND da.account_kind=CASE WHEN p.phase='reservation' THEN 'available' ELSE 'reserved' END
        JOIN payment.ledger_accounts ca ON ca.id=c.account_id AND ca.currency=p.currency AND (
            (p.phase='settlement' AND ca.wallet_id IS NULL AND ca.account_kind='clearing') OR
            (p.phase<>'settlement' AND ca.wallet_id=p.wallet_id AND ca.account_kind=CASE
                WHEN p.phase='reservation' THEN 'reserved' ELSE 'available' END))
        WHERE j.payment_id=p.id AND j.kind=p.phase AND j.currency=p.currency
    )
    UNION ALL
    SELECT 'unexpected_terminal_journal', p.id::text FROM payment.payments p
        JOIN payment.ledger_journals j ON j.payment_id=p.id
        WHERE p.state IN ('pending','needs_review') AND j.kind IN ('settlement','release')
    UNION ALL
    SELECT 'payment_outbox_mismatch', p.id::text FROM payment.payments p
        LEFT JOIN payment.payment_outbox o ON o.payment_id=p.id
        WHERE o.payment_id IS NULL OR o.done<>(p.state IN ('succeeded','failed'))
    UNION ALL
    SELECT 'reserved_balance_mismatch', a.id::text FROM payment.ledger_accounts a
        LEFT JOIN payment.payments p ON p.wallet_id=a.wallet_id
            AND p.state IN ('pending','needs_review')
        WHERE a.account_kind='reserved'
        GROUP BY a.id HAVING a.balance_minor<>coalesce(sum(p.amount_minor),0)
), issues AS (
    SELECT * FROM ledger_issues UNION SELECT * FROM payment_issues
)
SELECT jsonb_build_object(
    'issues', coalesce((SELECT jsonb_agg(to_jsonb(i) ORDER BY issue,reference) FROM issues i),'[]'),
    'metrics', (SELECT jsonb_build_object(
        'pending',count(*) FILTER (WHERE state='pending'),
        'needs_review',count(*) FILTER (WHERE state='needs_review'),
        'oldest_outbox_age_seconds',coalesce(max(greatest(0,
            floor(extract(epoch FROM statement_timestamp()-p.created_at))))
            FILTER (WHERE NOT o.done),0),
        'expired_leases',count(*) FILTER (WHERE NOT o.done AND o.lease_until<statement_timestamp())
    ) FROM payment.payments p LEFT JOIN payment.payment_outbox o ON o.payment_id=p.id)
)
"""
)


def snapshot(engine: Engine) -> Snapshot:
    with engine.connect() as connection:
        result: Snapshot = connection.execute(text(SNAPSHOT_SQL)).scalar_one()
        return result


def action(
    engine: Engine,
    payment_id: UUID,
    kind: Literal["lookup", "retry"],
    reason: str,
    correlation: UUID,
) -> str:
    return transaction(
        engine,
        lambda connection: str(
            connection.scalar(
                text("SELECT payment.operator_action(:id,:kind,:reason,:correlation)"),
                {"id": payment_id, "kind": kind, "reason": reason, "correlation": correlation},
            )
        ),
    )


def lookup(
    engine: Engine, provider: ProviderClient, payment_id: UUID, reason: str, correlation: UUID
) -> str:
    # Record the attempt before network I/O; an interrupted lookup remains auditable.
    action(engine, payment_id, "lookup", reason, correlation)
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT id AS payment_id,recipient,amount_minor,currency FROM payment.payments "
                    "WHERE id=:id"
                ),
                {"id": payment_id},
            )
            .mappings()
            .one()
        )
        payment = ProviderRequest.model_validate(row)
    return provider.lookup(payment)


def reconcile_external(
    engine: Engine, provider: LookupProvider, *, after: UUID | None = None, limit: int = 100
) -> ExternalReport:
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    with engine.connect() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT id AS payment_id,recipient,amount_minor,currency,state,updated_at "
                    "FROM payment.payments WHERE (CAST(:after AS uuid) IS NULL "
                    "OR id>CAST(:after AS uuid)) "
                    "ORDER BY id LIMIT :limit"
                ),
                {"after": after, "limit": limit + 1},
            ).mappings()
        )
    findings: list[Issue] = []
    for row in rows[:limit]:
        payment = ProviderRequest.model_validate({k: row[k] for k in ProviderRequest.model_fields})
        outcome = provider.lookup(payment)
        # No database transaction or lock spans HTTP. Concurrent progress is inconclusive,
        # not evidence of corruption; repeat this page after workers finish.
        with engine.connect() as connection:
            current = connection.execute(
                text("SELECT state,updated_at FROM payment.payments WHERE id=:id"),
                {"id": payment.payment_id},
            ).one()
        issue = None
        if current.state != row["state"] or current.updated_at != row["updated_at"]:
            issue = "concurrent_progress"
        elif outcome == "unknown":
            issue = "provider_unavailable"
        elif row["state"] in ("succeeded", "failed"):
            if outcome != row["state"]:
                issue = "provider_mismatch"
        elif outcome in ("succeeded", "failed"):
            issue = "awaiting_finalization"
        else:
            issue = "unresolved_reservation"
        if issue:
            findings.append({"issue": issue, "reference": str(payment.payment_id)})
    return {
        "findings": findings,
        "checked": min(len(rows), limit),
        "complete": len(rows) <= limit,
        "next_cursor": str(rows[limit - 1]["payment_id"]) if len(rows) > limit else None,
    }
