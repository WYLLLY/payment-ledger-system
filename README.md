# Payment & Ledger System

Milestones 1–4: FastAPI, PostgreSQL, identity, opaque sessions, wallets and an
immutable double-entry ledger with fixed dev/test openings and internal reconciliation.
Atomic internal transfers use retained idempotency responses and ordered account locks.
The [approved revised plan](docs/approved-plan.md) defines the scope. Payments
and workers (M5) are not implemented.

## Run locally

Requirements: Python 3.14, uv 0.12.23, Docker Engine with Docker Compose v2 or later.

```sh
python3 scripts/init_env.py
uv sync --frozen
docker compose config --quiet
docker compose up --build --wait --wait-timeout 180
curl --fail http://127.0.0.1:8000/health/live
curl --fail http://127.0.0.1:8000/health/ready
```

`init_env.py` creates independent random passwords in a mode-0600, ignored `.env`
and refuses to overwrite an existing file. `.env.example` documents the variables
without usable credentials. PostgreSQL is exposed on loopback port 15432; the API
on loopback port 8000. Compose waits for PostgreSQL, runs migrations and provisions
the application password, then starts the API. The database volume persists.

To run the API on the host after starting the database and migrations:

```sh
uv run --frozen uvicorn payment_ledger.main:create_app --factory --no-proxy-headers
```

Stop the stack with `docker compose down`. Adding `--volumes` **deletes its local
database**. The database initialization script runs only for a fresh volume;
changing `.env` does not rotate credentials on an existing database. Keep the
existing `.env`, or perform an explicit credential rotation with an administrator.

On this Mac, the verification engine uses the separate Colima profile
`payment-ledger`: `DOCKER_CONTEXT=colima-payment-ledger docker-compose ...` with
Homebrew's `/opt/homebrew/bin` on PATH. This does not change the default Docker
context. Restart it with `colima start --profile payment-ledger --activate=false`.

## Package boundaries

```text
src/payment_ledger/
  config.py       typed runtime settings and safe SQLAlchemy URL construction
  database.py     engine creation, connection timeouts and readiness probe
  health.py       HTTP routes, response schema and request dependency
  main.py         application factory and engine lifecycle
  api.py          identity and wallet HTTP routes, dependencies and safe errors
  services.py     application operations and transaction boundaries
  security.py     Argon2id passwords, random tokens and token hashing
  schemas.py      request validation and explicit response models
  models.py       SQLAlchemy persistence mappings; migrations own DDL
  errors.py       application errors translated by the HTTP layer
  ledger/         owner-scoped history and read-only internal reconciliation
  transfers/      validated transfer contracts and transactional service
migrations/       versioned schema and privilege changes
docker/           administrator bootstrap for a fresh PostgreSQL volume
scripts/          local secret generation and runtime credential provisioning
tests/unit/       configuration and HTTP contracts without PostgreSQL
tests/integration/ real PostgreSQL, role privileges and migration lifecycle
```

Modules have focused responsibilities. The engine is injected through FastAPI's
request dependency and disposed at application shutdown. Application services own
transactions; no repository interfaces or speculative class hierarchy are needed.
This keeps responsibilities and dependencies explicit.

`GET /health/live` reports process liveness without a database connection.
`GET /health/ready` checks connectivity as `payment_app`, the M2–M4 tables, required
column/table permissions and EXECUTE grants on M2/M4 functions. It
returns 200 with `{"status":"ok"}` or a generic 503 with
`{"status":"unavailable"}`. Errors are not echoed to clients or logs. Readiness
configures connect, server-side statement and pool wait timeouts. These are not an
absolute network deadline: an established TCP connection with a nonresponsive
peer can outlast them. The container probe has its own process timeout. A database
still at M1/M2/M3 or missing a required read/EXECUTE privilege returns 503.

## Database boundary and migrations

- PostgreSQL 17 is the supported server for this milestone; SQLAlchemy uses
  `READ COMMITTED` with psycopg 3.
- The administrator bootstrap creates `payment_migrator`, a non-superuser with
  `CREATEROLE`, and makes it database owner. This prerequisite cannot be created
  by a migration that must itself authenticate as that role.
- Alembic revision `0001_foundation` creates `payment_app`, `payment_worker` and
  the empty `payment` schema. Existing role attributes and memberships are
  checked before reuse. Use a dedicated project cluster: roles are cluster-wide.
- Only the separate migration container receives migration credentials. The API
  configuration fixes its username to `payment_app` and receives only its password.
  Password provisioning happens after the migration, outside migration source.
- M1 grants runtime database CONNECT and schema USAGE. M2 adds only the API's
  required reads, column-level inserts for users, sessions and audit events, and
  execution of wallet creation, session revocation and auth-counter functions.
  Runtime cannot directly write wallets/accounts/balances, update sessions,
  read or edit audit events, create schemas/TEMP objects or access migration metadata.
  New functions created by the migrator are not executable by PUBLIC.
  `payment_worker` remains NOLOGIN until a later approved milestone needs it.
- Downgrading to `base` removes the application schema and its runtime CONNECT
  grants. It intentionally retains cluster-wide roles and restrictive defaults;
  deleting them could affect another database. No `CASCADE` is used.
- Revision `0002_identity_wallets` follows `0001_foundation`. Tests retain the
  empty-schema M1 check at its original revision and cover fresh base → head,
  approved M1 → M2, repeated head, M2 → M1 → M2 and head → base → head.
  Downgrading M2 removes its identity and wallet data; use only on disposable databases.

Run migrations with `uv run --frozen alembic upgrade head`. Migration execution is
online only because role and database validation requires PostgreSQL. Application
startup never applies migrations. Deployment must serialize migrations; concurrent
migration runners are outside the supported workflow.

## Identity and wallets (M2)

| Endpoint | Behavior |
| --- | --- |
| `POST /auth/register` | Email and password → user, 201; duplicate normalized email → 409 |
| `POST /auth/login` | Email and password → opaque Bearer token and expiry |
| `POST /auth/logout` | Revoke the presented session, 204 |
| `GET /users/me` | Current authenticated user |
| `POST /wallets` | Currency EUR, USD or JPY → wallet and two zero accounts, 201 |
| `GET /wallets` | Current user's wallets |
| `GET /wallets/{wallet_id}` | Owned wallet; absent or foreign → identical 404 |

Protected routes require `Authorization: Bearer <token>`. Passwords are 12–128
characters at registration; email addresses are validated, ASCII-only and normalized
to lowercase. Argon2id uses 64 MiB, three iterations, four lanes and a random salt.
Tokens use 32 cryptographically random bytes; PostgreSQL stores only SHA-256
digests. Login errors are identical for unknown users and wrong passwords; an
unknown user still incurs Argon2 verification. Tokens/passwords never enter audit
events, error payloads or application logs. Use HTTPS for any non-local deployment.

Session lifetime defaults to 3600 seconds (`APP_SESSION_TTL_SECONDS`, 60–86400).
Logout revokes only the presented session. Requests authenticated before a logout
may finish; later session checks reject it. There are no cookies, JWT, refresh tokens,
email verification, password recovery or account-deletion flows in this milestone.
Registration's 409 intentionally reveals that a normalized email already exists.

`payment.create_wallet` inserts the wallet, its `available` and `reserved` accounts
and an audit event in one transaction. Uniqueness plus a deferred constraint trigger
require exactly two accounts; a composite FK requires the wallet's currency.
Balances are BIGINT, initially zero and nonnegative. Runtime has no direct balance
write permission. M3 adds controlled dev/test opening journals; M4 adds internal
transfers. Payments remain outside the implemented scope.

Registration and login share an atomic PostgreSQL fixed-window counter per direct
client IP:
20 requests per 60 seconds by default, configured with `APP_AUTH_RATE_LIMIT` and
`APP_AUTH_RATE_WINDOW_SECONDS`. Failed authentication consumes budget; exhaustion
returns 429 and `Retry-After`. Logout remains available for a valid session even
after this anonymous quota is exhausted. Uvicorn ignores forwarded headers in
Compose and the documented host command. A future proxy deployment needs an explicit trusted-proxy
policy. Clients sharing an IP share a budget; a fixed window permits boundary bursts
and is not a distributed-attack defense. Counters, expired sessions and audits do not
have automatic retention cleanup in M2.

Password hashing/verification occurs outside write transactions. Successful state
changes and their audits commit together; login failures have a separate audit
transaction. The API database role remains a trusted boundary, as in the approved
plan: it can act for users via granted operations; this is not per-user DB isolation.

## Verification

```sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest tests/unit
docker compose up --build --wait --wait-timeout 180
uv run --frozen pytest tests/integration
uv run --frozen pytest
uv run --frozen python scripts/smoke_api.py
actionlint .github/workflows/ci.yml
shellcheck docker/init-db.sh
```

Integration tests require the Compose stack and `.env`; missing infrastructure
fails tests rather than silently skipping them. They create uniquely named
`payment_test_*` databases, migrate as the real migrator and authenticate as the
runtime login. Databases are removed in fixture cleanup. Use only a dedicated
development/test cluster; do not run against a demonstration or production server.
The administrator credential is confined to test database creation/cleanup and
opening worker test sessions, which immediately reduce privileges with
`SET SESSION AUTHORIZATION payment_worker` because that role is NOLOGIN.

GitHub Actions installs the frozen lockfile, runs lint/format/types/unit tests,
builds the real Compose stack, tests PostgreSQL migrations and privileges, then
probes the running HTTP server. CI generates its own ephemeral credentials and
removes its volumes. Pull requests run the same verification.

The HTTP smoke script targets the local Compose API on port 8000. It checks the
identity/wallet/ownership/logout flow and transfer rejection/replay/conflict; it leaves
two dummy users, two zero-balance wallets and one retained idempotency record; its
sessions are revoked. Run it only against the development stack.

See [Milestone 1 verification](docs/milestone-1-verification.md) for commands,
observed results, independent review and remaining limitations.
See [Milestone 2 verification](docs/milestone-2-verification.md) for M2 evidence.

## Ledger and fixed fixtures (M3)

`0003_ledger` adds `ledger_journals` and `ledger_entries`, plus one `funding` and
one `clearing` system account per currency. Only system accounts may be negative.
Entries are nonzero BIGINT values bounded to ±1,000,000,000,000 minor units;
projected balances use the full BIGINT range and overflow aborts the transaction.
Composite foreign keys enforce the journal/account currency. Deferred constraint
triggers require exactly two distinct accounts with opposite amounts at commit,
including for an empty journal. UPDATE, DELETE and TRUNCATE triggers protect
history; runtime roles have no direct writes to journals, entries or balances.

The private `post_opening_pair` function validates ownership, currency, account
kinds and bounds, then locks accounts individually in ascending UUID order. It
inserts the pair and audit in the caller's transaction. An insertion trigger applies
each delta with a conditional debit; PostgreSQL enforces nonnegative user balances.
A unique wallet reference permits only one opening. A failed second line, audit,
commit or overflow rolls everything back. M3 enables only the `opening` journal
kind; later milestones must add their typed business operations and references.
No financial function is executable by API/worker. SECURITY DEFINER functions
fix `search_path=pg_catalog,pg_temp` and fully qualify application objects.

`GET /wallets/{wallet_id}/ledger?limit=50&offset=0` returns only the authenticated
owner's lines, without counterpart or system account identifiers. Limits are 1–100,
offset 0–1,000,000. Foreign and missing wallets both return 404. Existing wallet
reads display projected balances. There is no HTTP funding endpoint.

### Dev/test fixtures only

Use a dedicated dev/test cluster. **Do not run this provisioning command in the
demonstration deployment.** Normal Compose startup and Alembic never create
`payment_dev` and never grant funding capabilities to the API or worker.
Roles are cluster-wide; a database on a cluster previously used for fixtures may
see the role but receives no opening grant through migrations.

```sh
# Supply your own local password via DEMO_PASSWORD without committing it.
PAYMENT_LEDGER_DEV_FIXTURES=1 make seed-demo
make test-ledger
make reconcile
```

`DEMO_PASSWORD` must contain 12–128 characters. The explicit seed command uses
migration credentials to provision a restricted NOLOGIN `payment_dev` role and
fixed users `alice.demo@example.com` / `bob.demo@example.com`. It creates one
EUR/USD/JPY wallet for each, then switches to the dev role to execute openings.
Each EUR/USD wallet receives 100,000 minor units and each JPY wallet 10,000;
the function accepts no arbitrary amount. Initial balances and audits are atomic.
The batch prelocks all participating accounts in global UUID order. Reruns
serialize using a transaction advisory lock and preserve existing balances,
passwords and journals. A direct duplicate opening fails rather than funding twice.
Migration credentials stay in this local CLI, never in the API process.

`make reconcile` uses the restricted application role and a single SQL statement
(snapshot) to compare all projected balances to the ledger, verify pairs and
per-currency conservation, and detect negative user balances. It prints JSON and
exits 0 for PASS, 1 for inconsistencies, 2 for a database error. It never repairs
history or balances. Provider/payment reconciliation belongs to later milestones.

An empty M3 ledger supports M3 → M2 → M3. Downgrade refuses a populated ledger,
preserving financial history. An administrator or compromised migrator can disable
triggers/change schema; these guarantees intentionally exclude that trust boundary.

See [Milestone 3 verification](docs/milestone-3-verification.md) for evidence.

## Atomic internal transfers (M4)

| Endpoint | Behavior |
| --- | --- |
| `POST /transfers` | Atomic transfer; requires `Idempotency-Key`, returns 201 |
| `GET /transfers/{transfer_id}` | Transfer initiated by the authenticated user; otherwise 404 |
| `GET /transfers?limit=50&offset=0` | Paginated transfers initiated by that user |

Request body:

```json
{
  "source_wallet_id": "<your-wallet-uuid>",
  "destination_wallet_id": "<recipient-wallet-uuid>",
  "currency": "EUR",
  "amount_minor": 1500
}
```

Amounts must be JSON integers from 1 through 1,000,000,000,000 minor units;
booleans, floats and numeric strings are rejected. Wallets must be distinct and
use the requested currency. Only the source owner can initiate the transfer;
recipients see their credit through their own ledger history. Requests and reads
of an unauthorized source/transfer return the same 404 as an absent resource.
There is no currency conversion or direct reserved-account access.

Keys contain 1–128 visible ASCII characters, scoped to `(user_id, operation_kind,
key)`. PostgreSQL computes SHA-256 over canonical JSONB containing version 1, typed
source/destination UUIDs, integer amount and currency. Equivalent UUID spelling or
JSON field order does not change the hash. Concurrent duplicates wait on database
uniqueness and then return the exact stored status/body. An identical replay returns
201 and the original transfer ID/timestamp/journal ID; a changed payload returns 409.

The claim, transfer, journal, two lines, projected balances, audit and stored response
commit together. Expected business rejections (404, 409, 422) are also retained:
a rejected insufficient-funds request remains rejected after a later credit; use a
new key for a new attempt. Authentication/request-shape validation happens before
claiming the key. Technical errors roll back the claim and every monetary effect.
A 503 or lost response can be retried by the client using the **same** key.

The service retries the complete local transaction at most three attempts, only
for PostgreSQL deadlock/serialization-abort SQLSTATEs `40P01` and `40001`. It never
blindly retries connection loss or ambiguous commits. Account locks follow the
idempotency lock and are taken individually in ascending UUID order; balances are
read again after lock acquisition. A private pair-posting function is shared by
openings and transfers. Conditional debits, nonnegative CHECKs, pair triggers,
unique references and deferred transfer/idempotency completeness checks protect
financial state. Neither runtime nor dev roles can directly modify these tables
or call the private posting helper. Completed transfers and idempotency responses
are immutable and retained.

`0004_transfers` preserves existing M3 openings. An empty M4 schema supports
M4 → M3 → M4; downgrade refuses any retained transfer/idempotency history, including
rejected requests. No cleanup deletes financial keys. Reconciliation remains read-only.

```sh
make test-transfers
make test
make reconcile
```

See [M4 verification](docs/milestone-4-verification.md) and
[independent review](reviews/M4-review.md) for the recorded evidence.
