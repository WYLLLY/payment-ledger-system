# M4 — Independent review

Date: 11 October 2026. Verdict: **APPROVE**.

Reviewer: separate agent `m4_independent_review`, using `adversarial-review` and
`financial-invariant-review`. The reviewer changed no files or Git state.
Scope: M4 diff from merged M3 (`fbc62d1`), including migration, PostgreSQL posting,
transaction service, API, authorization, retries and tests.

## Findings and resolution

No blocking financial or security defect identified. Initial review noted that a
start barrier alone does not prove overlap at the database locks. The implementation
agent added tests that hold the lower UUID account, observe both transfer queries
waiting in PostgreSQL, and prove the higher account remains free using `NOWAIT`.
Duplicate-key tests now observe the waiting claimant and exercise both commit and
rollback of the first transaction. The reviewer independently verified these changes.

## Properties examined

- Unique user/kind/key claim; database-computed canonical hash; immutable retained
  response; conflicting payload returns 409 without changing the original result.
- Claim, transfer, journal, pair, projections, audit and response commit together;
  the service returns only after commit succeeds.
- Idempotency lock precedes account locks; account UUID order is deterministic;
  balances are read again after waiting.
- Positive bounded integer amounts, same-currency pair, nonnegative user balances,
  unique transfer journal and deferred completeness checks.
- Private posting helper, no runtime/dev direct writes, safe function search paths,
  SQL source-owner validation and owner-scoped API reads.
- At most three local attempts for SQLSTATE 40001/40P01; no automatic retry for
  connection failure; replay uses the original financial idempotency key.
- M3 openings survive upgrade; downgrade refuses retained M4 history.

## Independent execution

```sh
.venv/bin/pytest -q tests/integration/test_transfers.py \
  -k 'account_contention or duplicate_waits or connection_failure or each_supported_currency or rolls_back or financial_tables or direct_runtime' \
  --tb=short
```

**15 passed, 39 deselected in 10.84s**, on real PostgreSQL. The 39 deselections are
the explicit independent-review subset, not skipped acceptance checks; the complete
suite is recorded in `docs/milestone-4-verification.md`.

The initial attempt encountered 15 setup errors because PostgreSQL was stopped.
After the existing Colima profile restarted, the same command passed. No mock,
disabled assertion or relaxed invariant replaced a PostgreSQL check.

This agent review is independent evidence, not a GitHub approval by the PR author.
M4 still requires explicit user approval before merge. M5 is outside this review.
