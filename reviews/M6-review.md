# M6 — Independent adversarial and financial-invariant review

Date: 2026-10-12. Verdict: **APPROVE**.

Independent reviewer: separate `m6_independent_review` agent. Skills applied:
`adversarial-review` and `financial-invariant-review`; AGENTS.md and the approved plan
were read before reviewing. Review base: `origin/main` / HEAD
`0df1832c6a2f4e42d7be31a3867b0b6b9393eb77`, with the M6 working-tree implementation on
`feature/m6-resilience`. This review authorizes no merge. Full-suite, lint, type-check,
Compose and remote CI evidence remain the implementation agent's verification responsibility.

## Scope

Inspected migration 0006 (upgrade/downgrade, ACLs, counters, operator audit and correlation),
API admission, settings/readiness, structured logging middleware, transaction correlation,
worker/provider correlation, operator service/CLI and credential provisioning, M6 tests,
Compose/Docker logging configuration and `docs/operations-runbook.md`. Existing M3–M5
posting, idempotency, reservation, claim/finalize, provider persistence and recovery
mechanisms were inspected where the M6 paths rely on them. Existing uncommitted M4/M5
review reports were left untouched.

No unresolved Critical, High or Medium finding was identified. One Low observation was
reported to the implementation agent, corrected, and independently verified below.

## Corrected finding R1 — missing correlation on crash exhaustion

- Severity: Low.
- Location: `payments/worker.py:run_once` and `claim_payment`, addressed in
  `migrations/versions/0006_resilience.py:correlate_payment_audit`.
- Scenario: five claims crash without finalization; the next claim changes `pending` to
  `needs_review` before `run_once` installs the payment correlation context.
- Impact: the audit event originally had NULL correlation, weakening incident navigation.
  Reservation, retry budget, fencing and money remained correct.
- Cause: the claim transaction precedes setting the Python payment context.
- Original coverage: normal API correlation existed; the crash-exhaustion transition was
  not asserted for correlation.
- Correction: a restricted BEFORE INSERT trigger supplies the payment resource UUID only
  when a payment event has no correlation. Existing API request correlation is preserved.
- Required regression, now present and passing:
  `test_crash_exhaustion_audit_keeps_payment_correlation`. Existing request-isolation,
  migration and real CLI tests also passed after correction.

## Financial and concurrency analysis

| Property | Concrete protection inspected |
| --- | --- |
| No creation/destruction of funds, balanced two-entry journals | Existing PostgreSQL posting function, deferred journal checks, same-currency references, atomic projected-balance triggers; M6 adds no posting path. |
| No negative user balance or double spend | Existing nonnegative CHECK, ordered account row locks and transaction boundaries. Rate admission is separate from these financial guarantees. |
| Immutable ledger | Runtime/operator roles have no direct financial DML; existing mutation triggers remain. Operator receives diagnostics SELECT and its narrow action function only. |
| One reservation and one terminal effect | Existing payment phase uniqueness, terminal exclusivity index, deferred payment consistency and atomic terminal posting/state transition remain unchanged. |
| Safe operator recovery | `operator_action` locks outbox first, audits in the same transaction, never changes generation, attempts, payment state or ledger, and returns busy for a live lease. |
| Stale worker exclusion | Claim/finalize use the same outbox-first order, generation and lease checks. Operator scheduling cannot revive an expired worker's lease. |
| Unknown results retain funds | Operator can only request lookup/scheduling; `needs_review` recovery queries only. Only matching provider terminal evidence reaches the existing finalizer. |
| API/provider idempotency | Existing retained user/kind/key uniqueness and stored results remain. Provider key derives from immutable payment identity and persistent simulator outcome. Committed API replays bypass quota, preserving replay/conflict semantics. |
| Bounded concurrent quota | Fixed-window table primary key plus UPSERT serializes increments; per-user/per-operation counters, bounded values and persisted admission protect concurrent HTTP requests. |
| Reconciliation without repair | A single internal SQL statement provides a consistent snapshot; external HTTP is outside connections/transactions and subsequent state change is explicitly inconclusive. Operator lacks ledger DML/finalizer rights. |
| Safe audit/logging | Immutable operator audit, database `session_user`, validated reason, transaction-local request context, UUID IDs, route templates and selected event fields; no headers, bodies, query strings or raw SQL exceptions in events. |

Important transaction boundaries were checked: financial admission commits separately;
financial creation retains the existing atomic business/idempotency/ledger/audit transaction;
operator scheduling and audit commit together; claim commits before HTTP; terminal posting
and payment/outbox/audit commit together. Diagnostics do not keep a transaction open over HTTP.

## Independently executed verification

| Command/check | Result |
| --- | --- |
| `.venv/bin/pytest tests/integration/test_resilience.py tests/unit/test_observability.py -q --tb=short` | 52 passed, 0 failed, 0 skipped, 20.00s, initial review revision. |
| `PYTHONPATH=. .venv/bin/pytest -c pyproject.toml /tmp/test_m6_independent_probes.py -q --tb=short` | 5 passed, 0 failed, 0 skipped, 1.88s. |
| Targeted post-correction pytest run: crash-exhaustion correlation, API correlation isolation, M5 upgrade/operator login, real operator CLI commands, M6 readiness/ACL requirement | 5 passed, 0 failed, 0 skipped, 3.72s. |
| `git diff --check` | Passed. |

The last targeted run adds three newly implemented tests and repeats two affected tests;
these runs cover **55 distinct repository tests**, plus **5 independent probes**. They are
not a substitute for the required complete M1–M6 run. Temporary independent probes were
kept outside source at `/tmp/test_m6_independent_probes.py`; local outputs are in
`.local/m6-independent-tests.log`, `.local/m6-independent-probes.log` and
`.local/m6-review-corrections.log`. An initial probe invocation without `-c` passed but
reported unregistered-marker warnings because its root was `/tmp`; the recorded invocation
uses the repository's strict pytest configuration and passes without those warnings.

Independent probes establish:

1. Six simultaneous operator retries produce six audit records but no extra claim attempts
   or financial effects; subsequent worker settlement produces one terminal journal.
2. A lookup interruption after audit commit leaves a durable `requested` audit and the
   complete reservation, with no extra journal.
3. CLI database connection failure exits 2 with generic JSON, no dummy credential and no
   traceback.
4. Repeated crashed claims retain the reserve; this exposed R1, subsequently corrected.
5. Administrator-injected deletion of reservation history is reported as a payment-journal
   mismatch; diagnostics do not restore rows or alter balances.

Repository tests additionally exercise deterministic operator/worker lock contention,
stale generations, actual backend termination before operator transaction rollback,
lookup-only recovery from `needs_review` for success/refusal, remote/local disagreement,
concurrent-progress classification, pagination and no HTTP transaction, denied role
operations, immutable audit, retained-audit downgrade refusal, real operator login and
migration/readiness behavior.

## Documented limits assessed

The runbook accurately states the remaining boundaries: fictional funds, trusted database
runtime roles, shared operator principal with incident-ticket attribution, no force-release
or retry-budget reset, fixed-window bursts and pre-commit duplicate admission consumption,
paginated external observations rather than a distributed snapshot, no automatic ledger
repair, socket inactivity timeouts rather than total-response deadlines, native server
lifecycle logs, and retained cluster roles. These are explicit limits, not evidence of
broken financial invariants or unapproved infrastructure.

**Final verdict: APPROVE.** R1 is resolved. No implementation code was modified by this
reviewer; only this independent report was written inside the repository.
