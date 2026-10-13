# M4 — Independent review, 11 October 2026

## Findings

**No findings.** No actionable P0, P1, P2 or P3 defect introduced by the reviewed
change was confirmed. No implementation correction is required by this review.

**Verdict: APPROVE. Verification status: PASS.**

The transfer transaction, retained idempotency result, ledger constraints and
ordered locking satisfy the approved M4 acceptance criteria within the documented
database trust boundary. The complete M1–M4 suite and eight additional adversarial
scenarios passed. This assessment does not authorize merging the PR.

## Target and review method

- PR: [#2 — feat: add atomic idempotent transfers (M4)](https://github.com/WYLLLY/payment-ledger-system/pull/2).
- Reviewed HEAD: `77a63308d6dbba1f669124d1614f3a73deab90c5`.
- Base/merge base: `fbc62d16b02e275af835f05270b17ccb17ee08ee` (merged M3).
- Branch: `feature/m4-transfers`; working tree initially clean.
- Local `main` was behind its upstream. The comparison used `origin/main` and
  `git merge-base HEAD origin/main`, which matched the PR base.
- The entire 14-file PR patch matched the local base-to-HEAD diff after
  normalization of hunk context labels. No source changes occurred during review;
  HEAD and clean status were checked again when work resumed.

Read `AGENTS.md`, `docs/approved-plan.md`, the PR description, complete diff,
surrounding M1–M3 migrations/services/tests, and the `review-agent`,
`adversarial-review`, `financial-invariant-review` and `verification` skills.
This review was performed directly by the reviewing agent, independently of the
M4 implementation, without further delegation, following `review-agent`.
The earlier implementation-stage report is preserved below as historical context,
not used as proof of the executions reported here.

Only this report was edited in the repository. No implementation or versioned
test changes, commits, pushes, GitHub review submissions or merges were made.
Exploratory fault injection used disposable PostgreSQL databases, not the main
development database. No next-milestone implementation was started.

## Financial and transactional assessment

| Property | Concrete protection and inspected locations | Verification |
| --- | --- | --- |
| Atomic transfer and response | `transfers/service.py:31` opens one transaction; SQL `create_transfer` claims the key, creates the transfer/journal/pair, projects balances, appends audit and stores the response; service returns after commit (`:50`). | Failures on credit, audit, response and deferred commit leave no key or monetary effect; real backend termination also rolls back. |
| Exactly one effect per key | `0004_transfers.py:22`, `:164–170`: unique `(user_id, operation_kind, key)`, INSERT ON CONFLICT and row lock; no unprotected check-then-insert. | Duplicate waiter after holder commit returns the original result; after rollback it claims the key and posts once. |
| Canonical request and 409 conflicts | `0004_transfers.py:160–177`: server-computed SHA-256 over versioned JSONB and typed UUID/integer/currency fields; changed payload returns 409 before financial posting. | Each changed request field, equivalent UUID spelling, user scoping, concurrent conflicting requests and deterministic commit/rollback conflict cases. |
| Durable replay semantics | Response status/body retained and immutable after completion (`0004_transfers.py:75–112`); replay precedes fresh balance checks. Business rejections are retained as documented. | Original response survives later balance changes, a fully drained source, and an injected lost acknowledgement after a real commit. |
| Double-entry conservation | Shared private `post_pair` (`0004_transfers.py:55–73`), existing M3 pair triggers, composite currency FKs and bounded nonzero BIGINT entries. | Both source debit and recipient credit agree with the transfer; all three currencies and reconciliation pass. Maximum amount 10^12 succeeds without a duplicate debit. |
| Linkage and completeness | Unique idempotency reference, unique transfer journal, `ck_journal_operation`, and deferred `check_transfer_complete` (`0004_transfers.py:114–139`) check source ownership, response transfer ID and the exact debit/credit accounts and amounts. | Incomplete keys/transfers cannot commit; failure injection rolls back the complete operation. |
| Nonnegative balances and overflow | Accounts lock before balances are reread (`0004_transfers.py:192–205`); M3 conditional projection and user CHECK remain active. Destination addition uses NUMERIC before comparing with BIGINT maximum. | Two competing 60,000 debits against 100,000 allow only one success; destination overflow returns a retained 409 without a debit. |
| Concurrency and deadlocks | Idempotency row precedes account locks; accounts lock individually in ascending UUID order. The shared posting helper follows the same order. | Crossed transfers, observed lock contention and NOWAIT assertions pass. An actual PostgreSQL deadlock against an intentionally inverted test blocker produces 40P01 and one successful full-transaction retry. |
| Bounded retries and ambiguous outcomes | `transfers/service.py:16–17,51–56`: at most three attempts, only 40001/40P01; no automatic replay on connection loss or statement timeout. | Retry/exhaustion tests, real deadlock, real lock timeout, and injected acknowledgement loss after a durable commit. Client replay with the same key returns the retained result. |
| Immutable financial history | M3 ledger mutation/truncation triggers retained; M4 transfer and completed-idempotency mutation/deletion/truncation guards; no runtime direct writes. | Role-denial tests and owner-level mutation attempts pass; populated downgrade refuses to erase retained history. |

Exact transaction boundary: authentication and request-shape validation happen
before the transfer service transaction. The service validates a successful stored
body before committing, and returns it only after the transaction context exits
successfully. No external HTTP request occurs in this transaction. A technical
failure rolls back the claim and financial writes. Expected business rejections
commit their retained response without creating a transfer. A replay returns the
same stored result rather than reevaluating current business circumstances.

## Security, migrations and M1–M3 regressions

- HTTP authentication supplies the owner; PostgreSQL independently verifies source
  wallet ownership. Foreign and missing sources produce the same 404. Transfer
  reads are scoped to the initiating user; the recipient sees the credit through
  their own ledger history.
- Request amounts reject booleans, floats, numeric strings, zero, negatives and
  values over the bound. Keys require 1–128 visible ASCII characters. SQL values
  are bound parameters; callers cannot provide an unrelated request hash.
- API/worker/dev cannot directly modify financial tables or call private posting
  helpers. Only the API role receives EXECUTE on `create_transfer`; no PUBLIC
  EXECUTE grant is introduced. Privileged functions use a fixed search path and
  qualified application objects. API execution uses runtime credentials.
- Full suite execution includes fresh migrations, earlier revision upgrades,
  preservation of funded M3 openings, empty-M4 downgrade/upgrade, readiness ACL
  checks and refusal to downgrade retained M4 history. The opening function still
  satisfies M3 tests after adopting the shared pair helper.
- All 142 pre-M4 tests run unchanged alongside the 54 M4 cases. Authentication,
  sessions, wallets, ownership, rate limits, readiness, ledger immutability,
  privileges, reconciliation and earlier concurrency tests passed.
- Reconciliation continues to use one read-only SQL snapshot. No repair or
  historical rewrite is performed. Payments, outbox, provider outcomes and worker
  leases are outside M4 and were not represented as tested functionality.

## Commands executed and results

Commands ran from the repository root with the existing Python environment and
real PostgreSQL 17 on the `colima-payment-ledger` profile. PostgreSQL tests own
isolated databases and remove them afterward.

| Command | Observed result |
| --- | --- |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS; 54 files already formatted |
| `.venv/bin/mypy` | PASS; 40 source files |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS; 46 packages |
| `git diff --check` and `git diff --cached --check` | PASS |
| `.venv/bin/pytest -q --tb=short` | **196 passed in 122.40s**, no skips |
| `PYTHONPATH=. .venv/bin/python /private/tmp/m4_adversarial_review.py` | **8 additional scenarios passed**, exit 0; all eight temporary databases removed |
| `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose config --quiet` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS; image built, migration completed, DB/API healthy |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --wait --wait-timeout 180` | PASS after resuming the local environment |
| `.venv/bin/python scripts/smoke_api.py` | Final run PASS; actual HTTP health/auth/wallet/ownership/rejection/replay/conflict/logout |
| `.venv/bin/python -m payment_ledger.ledger.reconciliation` | PASS; `{"status": "PASS", "issues": []}` |

Suite breakdown: **16 unit tests + 180 real PostgreSQL integration tests**,
including **54 M4 cases** and the **142 existing M1–M3 cases**. The full run
includes the relevant API, migration, privilege, rollback and deterministic
concurrency tests; the additional script supplies targeted failure exploration.

Both remote `verify` checks on the reviewed PR HEAD were SUCCESS when read:
[push run](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38126303931)
and [PR run](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38126316850).
No new remote workflow run was triggered by this review.

## Additional failure scenarios

All eight scenarios below passed against real, disposable databases. The review
script was a temporary scratch artifact, not added to the repository; it was no
longer present in `/private/tmp` when the session resumed. The results above were
observed during this review, before that interruption.

1. **Actual backend termination before commit.** Post a complete transfer without
   committing, observe another same-key request waiting through `pg_blocking_pids`,
   confirm other readers see no partial state, then terminate only the original
   test backend. The contender succeeds with exactly one committed transfer,
   journal, audit and retained key; reconciliation remains clean.
2. **Lost commit acknowledgement.** Let the real DBAPI commit succeed, then inject
   an OperationalError at that return boundary. The service does not retry; the
   database contains one complete result. Replaying the same key returns the exact
   stored status/body with no second financial effect. This is boundary fault
   injection, not a physical network disconnect during COMMIT.
3. **Actual deadlock detection.** A test blocker holds the higher account UUID;
   the service locks the lower and waits for the higher. The blocker requests the
   lower, creating a real cycle. PostgreSQL aborts the service attempt with 40P01;
   releasing the blocker lets the second complete transaction succeed. Observed
   two service SQL attempts and one financial effect. The intentionally inverted
   blocker demonstrates recovery, not a defect in production lock ordering.
4. **Actual lock timeout.** Hold a required account, use a 100 ms statement timeout,
   and observe 57014 with no retained claim or monetary effect. After release, the
   same key succeeds and reconciliation is clean.
5. **Conflicting waiter after rollback.** Hold a 1,000-unit request uncommitted;
   observe a 2,000-unit same-key request waiting. Roll back the first request;
   the waiting request succeeds once for 2,000 units.
6. **Conflicting waiter after commit.** Repeat the previous setup but commit the
   first request. The waiting request returns 409; only the 1,000-unit transfer
   and original response remain.
7. **Maximum amount and fully drained source.** Transfer 10^12 minor units from
   a source funded to that exact amount. Same-key replay succeeds even though
   the source is now zero; a new-key attempt is rejected without another debit.
8. **Destination BIGINT boundary.** Use a synthetic near-limit projection in an
   isolated database. Overflow returns a retained `409 Balance limit exceeded`
   without creating a transfer. Restore the synthetic setup; the original key
   keeps its rejection, while a new key succeeds and reconciliation is clean.

## Environment incidents, coverage limits and recommendations

After work resumed, the first smoke command
`.venv/bin/python scripts/smoke_api.py && .venv/bin/python -m payment_ledger.ledger.reconciliation`
failed on `/health/live` with connection refused; the chained reconciliation
command therefore did not run in that attempt. `colima list` showed the profile
was stopped and the Docker socket was absent. Restarting the existing profile
with `/opt/homebrew/bin/colima start --profile payment-ledger` and starting Compose
resolved the environment failure. Both commands then passed. The reviewed HEAD
was unchanged, so the already successful full suite was not needlessly repeated.
Docker used its classic builder because buildx was unavailable; the build passed.

No required local M4 verification remains failed or skipped. No implementation
changes, relaxed assertions or mocks replaced PostgreSQL guarantees. The actual
HTTP smoke covers rejected transfers; funded success paths are covered through
TestClient and the service with real PostgreSQL, not through the live Compose
HTTP smoke. Physical network failure precisely during COMMIT and database-server
power loss were not exercised; the acknowledgement-loss test explicitly uses
fault injection after a real durable commit.

Non-blocking recommendation: retain the eight exploratory scenarios as versioned
regression tests in a subsequent authorized change, especially backend termination,
real deadlock recovery and post-commit acknowledgement loss. Their absence from
the checked-in suite is a coverage improvement opportunity, not a confirmed defect.
No corrective patch is proposed without an actionable finding.

The approved trust boundary remains material: a compromised application DB role
can invoke allowed functions for another supplied user; an administrator/migrator
can alter schema or disable constraints. The design expressly excludes these
stronger compromise scenarios. Dev/test and demonstration clusters must remain
separate because PostgreSQL roles are cluster-wide. APPROVE applies only to M4
at the reviewed commit and does not establish an absence of all possible defects.

---

## Historical implementation-stage review

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
