# M5 — Independent asynchronous payments review

Date: 12 October 2026 (Europe/Paris).

**Verdict: APPROVE. Verification status: PASS.**

The complete repository suite passed: **261 passed, 0 failed, 0 errors, 0 skipped**.
Fourteen additional independent adversarial probes passed: **14 passed, 0 failed,
0 errors, 0 skipped**. Combined successful execution: **275 passed**. No confirmed
implementation defect or material architectural deviation was identified.

## Scope and independence

- Reviewed [PR #3](https://github.com/WYLLLY/payment-ledger-system/pull/3),
  `feature/m5-payments`, exact head `0aaa0834dba8357019fcd41a225efe75969d74c9`.
  Local HEAD matched the live GitHub head. PR state was OPEN.
- Live base: `main`, `fbc62d16b02e275af835f05270b17ccb17ee08ee`.
  The PR includes M4 ancestry. M5-specific inspection used the boundary
  `77a63308d6dbba1f669124d1614f3a73deab90c5`; M4 PR #2 was also OPEN.
- Read current repository `AGENTS.md`, `docs/approved-plan.md`, the requested
  `adversarial-review`, `financial-invariant-review` and `verification` skills,
  and repository Git workflow guidance. The supplied `Documents/code` material
  was treated as reference instructions, not authorization to implement changes.
- Reviewed M5 migration, API/service, provider client, persistent simulator,
  worker, privileges/provisioning, readiness, deployment configuration, CI and
  tests; inspected inherited ledger and transfer protections and regressions.
- A separate reviewer, `independent_m5`, independently examined the financial,
  database, API and worker mechanisms and subsequently inspected the additional
  probes. Its final component verdict was APPROVE. The primary reviewer ran
  verification and wrote this report; the implementation agent's old report
  was not used as proof of current success.
- No implementation or repository test source was modified. Only this report
  was updated. The pre-existing uncommitted `reviews/M4-review.md` was preserved.
  No commit, push, GitHub approval, or merge was performed.

## Findings by severity

| Severity | Confirmed implementation defects |
| --- | --- |
| Critical | None |
| High | None |
| Medium | None |
| Low | None |

No corrective implementation patch is required by this review. The following
nonblocking coverage recommendation and operational limits are distinct from
confirmed financial or security defects.

### R1 — Preserve the additional regression coverage (Low, nonblocking)

**Location:** `tests/integration/test_payments.py`; review-only probes currently
in `/tmp/test_m5_review_probes.py`.

**Scenario:** A future change breaks USD/JPY finalization, concurrent conflicting
idempotency payloads, provider calls overlapping an expired lease, or terminal
outbox/audit rollback. The committed suite exercises several underlying pieces
separately but does not contain all the composed scenarios used in this review.

**Impact:** Future regressions in these combinations would have less direct CI
coverage. This review found no current failure in any of the combinations.

**Cause:** These fourteen additional cases were authored only as external review
probes to honor the request not to modify repository code.

**Existing coverage:** All fourteen passed against the current implementation.
The repository already covers basic currency constraints, duplicate arbitration,
fencing, provider duplicates, creation rollback and a crash before terminal
commit. The independent subreviewer inspected the added probe assertions.

**Recommendation:** Promote the probes into maintained regression tests in a
separately authorized code change. This is recommended hardening, not an approval
condition for the reviewed M5 implementation.

**Required test content for that improvement:** Preserve the six currency/outcome
combinations, conflicting-key race, both overlapping-worker windows, four terminal
write-fault cases, and late definitive refusal after retry exhaustion listed below.

## Financial invariants and concrete protections

Line references below refer to the reviewed commit.

| Property | Concrete protection and evidence |
| --- | --- |
| Atomic creation | `0005_payments.py:253–270` creates payment, reservation, outbox, audit and stored response within one DB function invocation; `payments/service.py:18–29` commits before returning. Injected outbox/audit/idempotency faults roll everything back. |
| API idempotency | `0005_payments.py:221–232`: canonical hash computed from typed parameters in PostgreSQL; unique `(user_id, operation_kind, key)`; `INSERT ON CONFLICT` then `FOR UPDATE`; retained immutable response. Identical replay preserves the initial 202/body, even after finalization; different payload returns 409. |
| Conservation and balanced journals | M3 `check_journal_pair` deferred triggers require exactly two opposite entries. Composite foreign keys bind entry/account/journal currencies; integer bounds avoid floats; NUMERIC reconciliation checks totals. |
| Nonnegative user balances | Shared posting locks accounts in ascending UUID order; the ledger insertion trigger updates projections atomically and refuses insufficient funds; user-balance CHECK provides another guard. Concurrent payment/payment and payment/transfer tests preserve funds. |
| Ledger immutability | Runtime roles lack direct ledger DML/TRUNCATE and private posting execution; immutable-entry/journal and no-truncate triggers protect retained history. M1–M4 privilege/invariant tests all passed under M5. |
| One reservation | `0005_payments.py:110–111` unique partial reservation index; deferred payment completeness verifies the exact available→reserved pair, amount and owner. |
| One terminal financial outcome | `0005_payments.py:112–113` unique partial index covers both settlement and release together. Payment identity/terminal state protection and deferred completeness enforce correspondence between state and financial outcome. |
| Atomic finalization | `0005_payments.py:342–348`: journal, reserved debit, clearing/available credit, terminal state, outbox completion and audit share a transaction. Both settlement and release roll back under injected terminal write failures. |
| Concurrent claims | `0005_payments.py:284–301`: `FOR UPDATE SKIP LOCKED`, due-time and lease eligibility, incremented generation, capped attempts. Both claim and finish lock outbox before payment, then ledger accounts as needed. |
| Stale workers | `0005_payments.py:312–316` verifies current generation, active lease, unfinished outbox and nonterminal state. Tests reject all stale outcomes and demonstrate rejection of the old worker after real overlapping HTTP. |
| Provider idempotency | `provider.py:25–27` derives the stable key from immutable payment UUID. `simulator.py:51–68` uses SQLite `BEGIN IMMEDIATE`, unique key/payment identity, matching parameters and immutable terminal outcome. WAL/FULL synchronization and commit precede response. |
| Unknown outcome retains funds | `worker.py:108–116` looks up status before any submission; only explicit absence and pending state permit the same-key submission. Timeout, 5xx, invalid/mismatched evidence become unknown; `0005_payments.py:317–329` never posts release for unknown. |
| Bounded execution and reconciliation | Five pending claims at most can submit; crashed claims consume the budget. Backoff/jitter schedules retries. Exhaustion becomes `needs_review` with funds reserved; subsequent claims only query and can settle/release on definitive evidence. Absence alone does neither. |
| Local retry correctness | `payments/service.py:18–29` retries only known transaction-abort SQLSTATEs 40001/40P01, at most three attempts. Connection/ambiguous-commit failures are not blindly replayed. Durable idempotency/outbox determine the next action. |
| Security boundary | API derives owner from authenticated session and filters reads; third-party resources return 404. The monetary function rechecks ownership. SECURITY DEFINER functions use `pg_catalog, pg_temp` and qualified objects, revoked PUBLIC execution, and split API/worker grants at migration lines 351–358. |

The DB worker role is intentionally trusted to report provider evidence through
its allowed finalization function; PostgreSQL cannot independently authenticate
remote HTTP outcomes. A compromised migrator, worker or simulated provider is
outside the stated protection boundary. No claim of protection against those
compromises is made.

## Transaction and crash boundaries

1. **Creation:** One `engine.begin()` encompasses idempotency arbitration,
   authorization/business checks, payment, reservation, projections, outbox,
   audit and stored response. Commit is the success point. Precommit faults
   leave no partial payment. Postcommit response loss can be retried using the
   same retained key; successful replay after finalization is explicitly tested.
2. **Claim:** Outbox/payment locks, lease, generation, attempt count and any
   `needs_review` transition commit before external communication.
3. **Provider:** Status lookup and optional submission occur without a borrowed
   DB connection, verified by pool instrumentation. SQLite commits the immutable
   result before replying. Lost replies do not imply refusal.
4. **Finalization:** A new DB transaction validates generation/lease/state,
   posts the one terminal pair and writes state/outbox/audit atomically. A killed
   PostgreSQL backend before commit leaves the reservation intact. Recovery
   queries the durable provider result and posts once.
5. **Recovery:** A worker crash before HTTP leaves a reclaimable lease. A crash
   after remote acceptance retains reserved funds until the next valid worker
   obtains terminal evidence. An obsolete worker cannot overwrite the successor.

## Requested failure scenarios and results

| Scenario | Execution and observed result |
| --- | --- |
| Crash before provider call | Repository `test_crash_recovery_persistent_provider[before_http]`: committed claim abandoned, lease expired, next worker recovers once. |
| Crash after remote acceptance | Repository after-remote case plus actual worker subprocess kill after SQLite commit, synchronized by Events; restarted recovery settles exactly once. |
| Crash before local commit | Real `pg_terminate_backend` after terminal SQL execution but before commit; reservation survives, later recovery succeeds. |
| Timeout before/after acceptance | Real HTTP simulator cases preserve reservation after unknown response and recover by lookup/same-key execution. |
| 5xx and unknown evidence | Real HTTP 503 case; twelve unit fault variants reject malformed, oversized, mismatched and protocol-truncated evidence. No unknown case releases funds. |
| Concurrent workers and expired lease | Committed SKIP LOCKED/terminal race/stale-generation cases, plus two live-worker review probes before and after remote acceptance. Old generation returned false; new generation true; one provider outcome and one terminal journal. |
| Definitive refusal | Repository HTTP decline and all three currency probes restore available funds and clear reserved funds, once. |
| Retry exhaustion | Five claims reach `needs_review`; funds stay reserved; lookup absence never resubmits or releases. Late success and additional late-refusal probe both finalize correctly. |
| Provider restart | Server/store recreated against the same SQLite file; previous success remains success even when restarted in decline mode. Conflicting parameters/identity rejected; concurrent duplicates produce one row. |
| Double settlement / settlement plus release | Partial unique terminal index rejects a second terminal phase; concurrent contradictory finalizers produce one winner; repeated and stale finalizers cannot add effects. |
| Terminal audit/outbox failure | Four review probes inject failures for both outcomes; state, balances, journal and audit remain uncommitted; subsequent status-based recovery succeeds once. |

## Complete verification results

All repository tests were selected, with no filters, exclusions, fail-fast setting,
skips or xfails. PostgreSQL-dependent guarantees used real PostgreSQL. The final
JUnit report records zero errors, failures and skips.

| Repository test group | Passed | Failed / errors | Skipped |
| --- | ---: | ---: | ---: |
| M1 foundation integration | 36 | 0 | 0 |
| M2 identity/wallet integration | 39 | 0 | 0 |
| M3 ledger integration | 51 | 0 | 0 |
| M4 transfers integration | 54 | 0 | 0 |
| M5 payments integration | 53 | 0 | 0 |
| Complete unit suite, including 12 M5 provider cases | 28 | 0 | 0 |
| **Repository total** | **261** | **0** | **0** |
| Additional independent review probes | 14 | 0 | 0 |
| **Combined** | **275** | **0** | **0** |

Repository suite duration: **82.23 seconds**. Probe duration: **7.99 seconds**.
M5-specific repository coverage is 65 cases (53 integration + 12 provider unit),
all included in the full run rather than counted twice.

### Additional probes

The temporary source `/tmp/test_m5_review_probes.py` reuses existing isolated DB
fixtures and real HTTP/SQLite helpers without changing repository sources:

- `test_each_currency_complete_flow`: EUR/USD/JPY × success/refusal, six cases;
  checks balances, clearing currency, terminal state, stable API replay and reconciliation.
- `test_concurrent_same_key_conflicting_payload`: barrier and observed PostgreSQL
  lock waits; exactly one 202 and one 409, one reservation and the winning amount.
- `test_two_live_workers_across_expiry`: two cases synchronized before/after
  provider acceptance; real overlapping worker executions, expired lease,
  one provider row, generation 1 rejected and generation 2 accepted.
- `test_terminal_write_failure_rolls_back_and_recovers`: settlement/release ×
  outbox/audit injected failures, four cases; verifies rollback before recovery.
- `test_needs_review_late_refusal_releases_once`: budget exhaustion retains funds,
  later definitive refusal restores funds and produces one terminal journal.

### Commands and checks

Commands ran from `/Users/william/Documents/Projet 1`. Test/network commands ran
with the host access needed for local PostgreSQL and loopback HTTP servers.

| Command | Result |
| --- | --- |
| `.venv/bin/pytest --junitxml=/tmp/m5-review-suite.xml > /tmp/m5-review-suite.log 2>&1` | 261 passed; all M1–M5 tests executed |
| `PYTHONPATH="$PWD" .venv/bin/pytest -c pyproject.toml /tmp/test_m5_review_probes.py --collect-only -q` | 14 collected |
| `PYTHONPATH="$PWD" .venv/bin/pytest -c pyproject.toml /tmp/test_m5_review_probes.py -q --junitxml=/tmp/m5-review-probes.xml > /tmp/m5-review-probes.log 2>&1` | 14 passed |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS; 67 files already formatted |
| `.venv/bin/mypy` | PASS; 50 source files |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS; 46 packages resolved |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose config --quiet` | PASS |
| `git diff --check` | PASS |
| `gh pr view --json number,url,title,body,baseRefName,headRefName,headRefOid,files,statusCheckRollup` and `gh pr checks` | PR #3/head verified; both remote CI verify checks SUCCESS |
| `gh pr view 3 --json state,baseRefOid,headRefOid,mergeStateStatus,reviewDecision` | OPEN, CLEAN; reviewed head unchanged; no GitHub review approval submitted |
| `gh pr view 2 --json state,headRefOid` | M4 dependency still OPEN at reviewed boundary |

**Migration verification:** Fresh isolated databases migrate to head for the
integration tests. Foundation tests exercise base→head, repeated upgrade and
empty downgrade/re-upgrade. M2/M3/M4 upgrade tests remain green. The M5 populated
M4→M5 test preserves transfers and checks readiness/privileges, empty M5→M4→M5
works, and populated payment history refuses destructive downgrade. Runtime API,
worker-login, permitted/forbidden role and ledger integrity tests passed.

**Remote CI:** Both checks on the PR were successful, with runs
[38168899379](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38168899379)
and [38168913002](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38168913002).
The workflow includes the complete integration suite and Compose HTTP/payment
smokes. These are remote observations, not a claim of a fresh local Compose smoke.

### Environment failures encountered and resolved

No unsuccessful invocation is counted as successful verification:

1. `uv run --frozen pytest --junitxml=/tmp/m5-review-suite.xml ...`,
   `uv run --frozen ruff check .`, `uv run --frozen ruff format --check .`, and
   `uv run --frozen mypy` could not start because `uv` was not on PATH. Used the
   existing `.venv/bin` executables; the frozen lock was independently checked.
2. The first `.venv/bin/pytest` invocation inside the restricted sandbox produced
   **28 passed, 1 failed, 232 setup errors, 0 skipped**. The standalone real-HTTP
   test could not bind a socket (`PermissionError: Operation not permitted`);
   DB fixtures reported PostgreSQL unavailable. Docker's dedicated profile was
   stopped. This was an environment failure, not a demonstrated code regression.
3. `/opt/homebrew/bin/colima start --profile payment-ledger` and
   `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose up -d --wait db`
   restored the existing dedicated DB. The identical full suite with appropriate
   host access then passed all 261 tests. Initial diagnostic artifacts are retained
   as `/tmp/m5-review-suite-environment.log` and `.xml`.
4. Initial sandboxed `gh pr view` could not connect to GitHub; the authorized
   network retry succeeded and supplied the live PR/CI evidence above.

No tests or checks were disabled to obtain passing results. Temporary logs,
JUnit files and probe source are local review artifacts and may be cleaned up by
the operating system; the durable evidence summary is this report.

## Missing coverage, regressions and remaining limits

- **No observed M1–M4 regression.** Existing integration files are unchanged by
  M5; all run against the current head and passed. M5 adds its two test files and
  extends the OpenAPI test without removing earlier assertions.
- **Permanent coverage gap:** The fourteen probes are not yet committed tests;
  see R1. They close the corresponding evidence gaps for this review execution.
- **Further nonblocking tests:** Direct SQL invalid/null/currency/recipient
  validation for `create_payment`, and process-level ambiguous-commit/response-loss
  tests for payment creation, would complement current SQL inspection, replay,
  creation rollback and inherited transaction tests. These are not claimed as
  separately executed scenarios.
- **HTTP deadline limit:** `provider.py:62` uses a socket inactivity timeout,
  not an absolute total deadline. A continuously trickling response can occupy a
  worker beyond its lease. Fencing/idempotency protect financial state, but
  recovery latency/worker availability can suffer. Consider a total request
  deadline in resilience work. This pre-existing documented limitation is not a
  newly observed financial defect; no slow-stream load test was run here.
- **Recovery scope:** `needs_review` lookup-only resolution is present in M5.
  Broader external discrepancy reporting, operator commands, structured logs,
  metrics and financial-route rate limits belong to approved M6. The internal
  reconciliation command verifies ledger consistency; it does not claim to be
  a complete external payment reconciliation dashboard.
- **Durability scope:** Provider restart/persistent outcomes and worker/DB-backend
  process failures are tested. Host power loss, SQLite-volume loss and PostgreSQL
  server crash durability were not tested. A provider-volume reset would erase
  idempotency history and lies outside the persistent-store guarantee.
- **Deployment scope:** Compose configuration and live GitHub CI were checked.
  A fresh local image rebuild and the two standalone Compose smoke scripts were
  not repeated; the local complete suite and additional probes used real DB and
  provider HTTP with the API through FastAPI TestClient. No required repository
  test was skipped.

## Final assessment

**APPROVE.** M5 satisfies the reviewed asynchronous-payment and minimum-recovery
criteria. Atomic reservations, retained idempotency, persistent provider results,
fenced workers and exclusive terminal journals have concrete database/application
protections and executed failure/concurrency evidence. Unknown outcomes retain
funds; definitive refusal releases them; recovery does not duplicate payment or
terminal posting. Full regression verification is green, and no significant
independent-review finding remains open. This verdict authorizes no merge and
does not declare M6 implemented or reviewed.
