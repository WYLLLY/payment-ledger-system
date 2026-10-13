# M6 — Resilience and operations verification

Date: 2026-10-12. **Local verification: PASS. Independent review: APPROVE.**

Branch: `feature/m6-resilience`. Implementation commit: `abbf52a`.
Base: `origin/main` at `0df1832c6a2f4e42d7be31a3867b0b6b9393eb77`.
M1–M5 are present in main: M1 `0edadff`, M2 `c47f027`, M3 merge `fbc62d1`,
M4 merge `eb42cce`, M5 merge `0df1832`. Their verification reports were inspected;
the unchanged M1–M5 baseline was rerun: **261 passed, 0 failed, 0 skipped** (88.71s).
Existing uncommitted `reviews/M4-review.md` and `reviews/M5-review.md` were preserved
byte-for-byte and excluded from M6 commits.

## Delivered scope

- Migration `0006_resilience`: separate restricted NOLOGIN operator role; validated,
  SECURITY DEFINER scheduling/lookup-audit function; immutable operator event history;
  PostgreSQL financial fixed-window counters; correlation on financial audit events.
- Optional separate operator login provisioner; runtime CLI commands `metrics`, `reconcile`,
  `lookup`, `retry`. No migration credentials in operator runtime settings, no public
  management endpoint, no ability to force a payment outcome or change a ledger entry.
- Single-statement internal snapshot detects ledger/projection, journal, payment phase,
  reserved balance and outbox discrepancies. External reconciliation uses validated
  provider identity, bounded pages and explicit concurrent-progress/unknown classifications.
- Read-only metrics: pending, needs_review, oldest incomplete outbox age, expired leases.
- Operator scheduling and its audit are atomic. A live lease is respected; attempts,
  generations and terminal states are never reset. Existing fenced workers execute recovery.
- JSON events, UUID request IDs, transaction-local audit correlation, payment UUID propagation
  to worker/provider, route-template logging, no request secrets or SQL exception output.
- Authenticated transfer/payment POST admission with independent per-user/per-kind counters,
  429/Retry-After, retained replay/conflict semantics for committed keys and readiness checks.
- Operator runbook, recovery procedures, configuration and Make targets. No additional
  service/database/queue infrastructure, no architecture deviation, no automatic merge.

The original financial posting/state-machine functions remain unchanged. Only validated
provider terminal evidence reaches settlement/release through the existing worker path.
Unknown outcomes and exhausted retries retain their reservations.

## Commands and evidence

Python 3.14 and locked dependencies; real PostgreSQL 17 under the dedicated
`DOCKER_CONTEXT=colima-payment-ledger` profile. Missing database infrastructure is a failure,
not a skip. Colima was restarted before baseline tests; no user data volume was deleted.

| Command/check | Result |
| --- | --- |
| `.venv/bin/pytest -q --tb=short` (final, after review correction) | **316 passed, 0 failed, 0 skipped**, 103.38s |
| Suite breakdown (collection checked) | **31 unit, 285 integration**; M6 adds 55 tests to the 261-test baseline |
| Earlier targeted M6/observability run | 52 passed before three final regression/CLI/readiness additions |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS, 77 files |
| `.venv/bin/mypy` | PASS, 59 source files |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS; migration exits 0, PostgreSQL/API/provider healthy, worker started |
| `.venv/bin/python scripts/smoke_api.py` | PASS; actual HTTP health, identity, wallets, ownership, transfer idempotency and logout |
| `PAYMENT_LEDGER_DEV_FIXTURES=1 .venv/bin/python -m scripts.smoke_payments` | PASS; actual reservation → worker → HTTP simulator → settlement, replay and reconciliation |

Local output: `.local/m6-baseline.log`, `.local/m6-targeted-2.log`,
`.local/m6-all-final.log`, `.local/m6-compose.log` (ignored diagnostic files).
The existing GitHub workflow executes all unit/integration tests, lint/format/types,
migration/Compose startup and both smoke flows. Remote CI is tracked on the M6 PR;
local results above do not stand in for remote completion.

## Failure, concurrency, security and migration coverage

The complete run retains all M1–M5 tests, including actual worker process termination after
provider acceptance, before-local-commit backend termination, lease expiry, competing workers,
stale generation rejection, persistent provider idempotency, monetary transaction rollback,
concurrent duplicate requests and competing debits.

M6 adds deterministic quota contention (8 synchronized requests, exact admission bound),
separate user/kind quotas, rollover, rejection persistence, committed replay/conflict at quota,
invalid SQL parameters, outbox lock contention with operator/worker, and stale worker fencing
after operator scheduling. Tests terminate an operator backend before commit, verify both
audit/scheduling rollback, repeat after uncertain command delivery and assert one terminal
financial effect. After exhaustion, remote absence retains funds; later success/refusal
settles/releases exactly once through the worker.

Reconciliation tests inject privileged corruption and assert detection without repair;
they distinguish remote contradiction from local progress, verify pagination and prove that
no database checkout spans external HTTP. JSON/correlation tests cover sensitive headers,
URLs, exception messages, request isolation and the crash-exhaustion audit event.

Privilege tests run as API, worker and operator: forbidden ledger/outbox/counter DML,
operator function isolation, denied finalization/escalation/credential reads, immutable audit
and real restricted operator login/CLI behavior. Migration tests cover base → head → base →
head, M5 → M6 → M5 → M6, repeated upgrades, readiness/EXECUTE revocation and refusal to
downgrade retained operator audit.

## Corrections and independent review

The initial M6-only run was **36 passed / 4 failed**. Three failures shared an ambiguous
PostgreSQL NULL pagination parameter, corrected with an explicit UUID cast. The fourth was
the crash test's missing expectation for the terminated backend's rollback exception.
No test or financial guarantee was disabled. The subsequent targeted run passed all 52 tests.

Independent adversarial + financial-invariant review is recorded in
[`reviews/M6-review.md`](../reviews/M6-review.md). It independently covered 55 distinct
repository tests and 5 additional probes (all passing). One Low finding was corrected:
crash exhaustion can transition to needs_review inside claim before Python knows the payment
identity. The audit trigger now supplies the payment UUID when correlation is absent, and
an explicit regression passes. The final complete 316-test run occurred after this correction.
**No open review findings; verdict APPROVE.**

## Explicit limits

See [`operations-runbook.md`](operations-runbook.md) for exact procedures. Provider timeouts
are socket inactivity limits, not a total-response deadline. Reconciliation pages are
observations, not a distributed atomic snapshot. Unknown/absent remote outcomes after retry
exhaustion remain reserved; there is no force release or budget reset. Financial admission
is an HTTP control under the trusted API role and permits fixed-window boundary bursts.
The operator principal is shared; incident reasons provide human/ticket attribution.
Server lifecycle logs retain native formats. No required verification was skipped.
