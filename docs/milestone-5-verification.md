# Milestone 5 — Payments and minimum recovery

Date: 11 October 2026 (Europe/Paris).

**Verification status: PASS locally. Independent review: APPROVE.**
M1–M5: **261 passed in 86.45s**, including 28 unit tests and 233 real PostgreSQL
integration cases, no skips. Remote CI status is tracked on the M5 pull request.
M6 was not started; no branch or pull request was merged.

## Scope and database changes

The approved M5 is implemented on `feature/m5-payments`, based on reviewed M4
commit `77a6330`. Before implementation, the M1–M4 baseline passed all 196 tests
in 57.81s. AGENTS.md, the approved plan and the four repository skills were read.
The requested `git-workflow` skill was copied byte-for-byte from the supplied
`Documents/code` source into `.agents/skills/git-workflow/SKILL.md`.

`0005_payments` adds payments and one outbox row per payment, retained payment
idempotency responses, reservation/settlement/release journal references,
unique reservation and terminal indexes, immutable identity/terminal states,
deferred payment completeness checks, and transactional transition audits.
Safe SECURITY DEFINER functions provide creation to the API and claim/finalize
to the worker. The private posting helper remains inaccessible to runtime roles.

Creation atomically claims the key, validates ownership/currency/balance, creates
the payment, reserves available → reserved, creates the outbox task, audits and
stores the initial response. Identical replays retain the original 202/body;
GET returns current state. Different payloads conflict. PostgreSQL enforces
money conservation and nonnegative user balances; account locks use UUID order.

Claim/finalize lock outbox then payment, before any ledger accounts. The worker
commits its claim before HTTP, uses the immutable payment-derived provider key,
and checks provider status before submission. Finalization validates the lease
and generation before posting reserved → clearing or reserved → available and
commits journal, state, outbox completion and audit together.

Unknown outcomes retain funds. Five claims at most may submit; claims lost to
crashes count toward this limit. Backoff/jitter bounds automatic retries. After
exhaustion, `needs_review` remains reserved and is queried once per minute;
absence never causes release or automatic resubmission. A later definitive
result can still finalize. Local transactions retry only `40001`/`40P01`, at
most three attempts; ambiguous commits are never blindly replayed.

The HTTP simulator persists key, immutable payment parameters and terminal
result in SQLite with `BEGIN IMMEDIATE`, WAL and FULL synchronization before
replying. The worker validates identity and parameters on provider evidence.
Compose runs API, worker and provider separately using the same application
image; only API declares its build to avoid concurrent tag creation. The
provider volume is persistent and non-root. The API and worker have separate
passwords; `payment_worker_login` inherits only the restricted NOLOGIN worker
role. Migration credentials reach neither runtime.

## Files

- Database: `migrations/versions/0005_payments.py`.
- Payment contracts/service, provider client, SQLite simulator and async worker:
  `src/payment_ledger/payments/`.
- API, M5 readiness and ledger history: `api.py`, `database.py`, `ledger/history.py`.
- Runtime provisioning and local secret upgrade: `scripts/provision_app.py`,
  `scripts/init_env.py`, `.env.example`, `compose.yaml`, `Dockerfile`.
- Tests: `tests/integration/test_payments.py`, `tests/unit/test_payment_provider.py`,
  plus the extended OpenAPI contract in `tests/unit/test_health.py`.
- Real Compose payment smoke: `scripts/smoke_payments.py`, gated by
  `PAYMENT_LEDGER_DEV_FIXTURES=1` for dedicated dev/CI clusters only.
- CI, Makefile, README, this report and `reviews/M5-review.md`.

No M1–M4 test was weakened or removed. The pre-existing modification to
`reviews/M4-review.md` remains unstaged and outside the M5 commits.

## Tests added

65 M5 cases cover request validation, ownership, replay after finalization,
conflicting payloads, concurrent same/different keys, observed PostgreSQL lock
waits, competing transfers/payments, two workers, SKIP LOCKED, expired leases,
stale generations, exclusive terminal journals, state/identity immutability,
allowed/forbidden roles and actual worker-login authentication.

Failure cases cover creation rollback, local transaction retry rollback, crash
before HTTP, after remote acceptance and before local commit, real PostgreSQL
backend termination, and actual worker-process kill after durable SQLite
acceptance. The last window is synchronized with Events rather than sleeps.

Real HTTP tests cover success, definitive refusal, timeout before execution,
timeout after acceptance, 503, bounded retries, lookup-only exhaustion,
eventual reconciliation, persistent restart and concurrent provider duplicates.
Unit cases reject malformed, oversized, mismatched and truncated HTTP evidence.
Pool checkout instrumentation proves HTTP runs with no DB connection borrowed.
Migrations cover fresh databases, retained M4 transfers → M5, empty M5 downgrade/
re-upgrade, readiness privilege failures and refusal to erase payment history.

## Commands and observed results

Executed in the project root; PostgreSQL/network commands required host access
outside the restricted execution sandbox.

| Command | Result |
| --- | --- |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS, 67 files |
| `.venv/bin/mypy` | PASS, 50 source files |
| `.venv/bin/pytest -q --tb=short` | 261 passed in 86.45s; 28 unit / 233 integration; no skips |
| `.venv/bin/pytest tests/integration/test_payments.py tests/unit/test_payment_provider.py -q` | Independent reviewer: 65 passed in 25.22s |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS, 46 locked packages |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `cmp /Users/william/Documents/code/.agents/skills/git-workflow/SKILL.md .agents/skills/git-workflow/SKILL.md` | Exact match |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180` | PASS after single-build correction; DB/API/provider healthy, worker running, migration exit 0 |
| `.venv/bin/python scripts/smoke_api.py` | PASS: actual HTTP identity/wallet/transfer/replay/conflict/logout flow |
| `PAYMENT_LEDGER_DEV_FIXTURES=1 .venv/bin/python -m scripts.smoke_payments` | PASS: actual Compose API → reservation → worker → HTTP provider → settlement, stable replay, zero reserved balance |
| `.venv/bin/python -m payment_ledger.ledger.reconciliation` | PASS, no issues |

The pytest migration fixtures execute Alembic as the separate migrator on fresh
isolated databases, including upgrades/downgrades, and clean up those databases.
The funded Compose smoke intentionally leaves one fictional user/wallet/payment
and immutable history; it revokes its session. No funding endpoint is exposed.

## Failures encountered and resolved

- Initial baseline test access failed in the sandbox (16 passed, 180 setup
  errors). Host access to the existing dedicated PostgreSQL stack allowed the
  unchanged M1–M4 suite to pass all 196 tests.
- After the session interruption, Docker/PostgreSQL were stopped. The first
  complete relaunch gave 29 passed / 232 setup errors; restarting the existing
  Colima profile and Compose resolved this environment failure. It is not
  counted as successful verification.
- Concurrent builds of the same Docker image tag failed with `AlreadyExists`
  under the classic builder. One API build now supplies the image for every
  service; a complete rebuild/start passed afterward.
- Independent review found uncaught HTTP protocol exceptions and a timing-based
  crash-test window. Both were corrected, tested independently and included in
  the successful full-suite rerun. See the independent review report.
- Initial Ruff/mypy diagnostics while writing new files were corrected without
  disabling lint/type rules or relaxing assertions.

## Remaining limits and scope

No required local verification remains failed or skipped. Remote GitHub Actions
must be checked on the final commit before claiming CI success. The simulator
is trusted local test infrastructure; a real provider integration is outside
this milestone. HTTP timeouts are per-socket inactivity timeouts, not absolute
total deadlines against a continuously trickling peer. No DB-server power-loss
experiment is claimed; process crashes and transaction rollback are exercised.

No approved architecture deviation was required. An additional LOGIN role that
inherits the existing restricted worker role preserves M1's NOLOGIN invariant
while providing a separately credentialed worker. Minimum recovery is in M5;
operator tooling, broader external reconciliation reporting, observability and
financial-route rate limiting remain M6.
