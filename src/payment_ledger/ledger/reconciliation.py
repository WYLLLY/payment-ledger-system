"""Read-only integrity report from one PostgreSQL statement/snapshot.

No repair is attempted. NUMERIC sums avoid overflow when adding BIGINT entries.
"""

import json

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError

from payment_ledger.config import Settings
from payment_ledger.database import create_database_engine

RECONCILIATION_SQL = """
WITH projections AS (
    SELECT a.id, a.balance_minor, COALESCE(sum(e.amount_minor), 0) AS ledger_balance
    FROM payment.ledger_accounts a LEFT JOIN payment.ledger_entries e ON e.account_id = a.id
    GROUP BY a.id
), journals AS (
    SELECT j.id, count(e.account_id) AS entry_count, COALESCE(sum(e.amount_minor), 0) AS total,
           count(DISTINCT e.currency) AS currencies,
           bool_and(e.currency = j.currency) AS matching_currency
    FROM payment.ledger_journals j LEFT JOIN payment.ledger_entries e ON e.journal_id = j.id
    GROUP BY j.id
), currencies AS (
    SELECT currency, sum(balance_minor) AS total FROM payment.ledger_accounts GROUP BY currency
)
SELECT 'balance_mismatch' AS issue, id::text AS reference
    FROM projections WHERE balance_minor <> ledger_balance
UNION ALL
SELECT 'invalid_journal', id::text FROM journals
    WHERE entry_count <> 2 OR total <> 0 OR currencies <> 1 OR NOT matching_currency
UNION ALL
SELECT 'currency_not_conserved', currency FROM currencies WHERE total <> 0
UNION ALL
SELECT 'negative_user_balance', id::text FROM payment.ledger_accounts
    WHERE wallet_id IS NOT NULL AND balance_minor < 0
ORDER BY issue, reference
"""


def reconcile(engine: Engine) -> list[dict[str, str]]:
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(text(RECONCILIATION_SQL)).mappings()]


def main() -> int:
    engine = create_database_engine(Settings())
    try:
        issues = reconcile(engine)
        print(json.dumps({"status": "FAIL" if issues else "PASS", "issues": issues}))
        return 1 if issues else 0
    except SQLAlchemyError:
        print(json.dumps({"status": "ERROR", "detail": "Reconciliation unavailable"}))
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
