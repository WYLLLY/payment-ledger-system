# M3 independent review — 2026-10-10

Reviewer: separate `m3_independent_review` subagent, not the implementing agent.
Skills: `adversarial-review` and `financial-invariant-review`. Read-only review;
no implementation files changed by the reviewer.

Scope: the original revised `docs/approved-plan.md`, M3 only; migration 0003,
posting/projection/constraints, runtime/dev privileges, fixture CLI, internal
reconciliation, ledger history, readiness and tests. M4 is not implemented.

## Finding Medium — fixture batch lock order (resolved)

The original seed opened multiple wallets in one transaction with locks sorted
only within each pair. A concurrent opening of the second wallet could lock its
available account and then wait on funding held by the seed; the seed then waited
on that available account. The reviewer independently reproduced PostgreSQL
`40P01` on an isolated database. Atomic rollback prevented financial corruption,
but one dev/test opening failed unnecessarily.

Correction: prepare all fixture wallets, gather the full set of participating
available/funding account IDs, and lock each in global UUID order before posting.
The transaction advisory lock also serializes concurrent seed invocations.

Regression: `test_seed_acquires_global_locks_before_posting` blocks the smallest
account, waits for the seed via `pg_blocking_pids`, and uses `NOWAIT` to prove the
higher funding account is still free. After releasing the blocker it verifies six
journals and a clean reconciliation. This detects the original implementation.

Independent execution after correction:

```sh
.venv/bin/python -m pytest tests/integration/test_ledger.py \
  -k 'seed_acquires_global or concurrent_conditional_debits or posting_locks_lower'
```

Result: **3 passed, 48 deselected in 2.10s**, PostgreSQL real, exit 0.
Reviewer conclusion: finding resolved; **no remaining significant findings**.

## Mechanisms examined

- Deferred journal/entry triggers reject empty, incomplete, extra or unbalanced pairs.
- Composite foreign keys require one currency across journals, entries and accounts.
- Atomic projection, conditional debit, user non-negativity, BIGINT overflow rejection.
- Rollback of journal, pair, balances and audit; unique opening per wallet.
- UPDATE/DELETE/TRUNCATE immutability and forbidden direct runtime/dev writes.
- Owner validation, restricted EXECUTE and fixed privileged `search_path`.
- No dev funding role/grants provisioned by normal migrations or Compose startup.
- Private owner-scoped paginated history; one-snapshot read-only reconciliation.
- M2→M3 upgrade, empty-ledger downgrade and populated-ledger downgrade refusal.

Limit: administrators/migrator can change schema or disable constraints; explicitly
outside the financial guarantee. Role objects are cluster-wide. The dev/test seed
command belongs only on a dedicated dev/test cluster.
