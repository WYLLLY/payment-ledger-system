# Payment & Ledger System

Milestones 1–2: a typed FastAPI application, real PostgreSQL, restricted database
roles, Alembic migrations and CI, with identity, opaque sessions and empty wallets.
The [approved plan](docs/approved-plan.md) defines later financial functionality.

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
`GET /health/ready` checks connectivity as `payment_app`, the M2 tables, required
column/table permissions and EXECUTE grants on M2 functions. It
returns 200 with `{"status":"ok"}` or a generic 503 with
`{"status":"unavailable"}`. Errors are not echoed to clients or logs. Readiness
configures connect, server-side statement and pool wait timeouts. These are not an
absolute network deadline: an established TCP connection with a nonresponsive
peer can outlast them. The container probe has its own process timeout. A database
still at M1 or missing a required M2 privilege returns 503.

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
write permission. No posting, funding, journals, transfers or payments exist yet.

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
removes its volumes. No Git remote has been configured by this implementation.

The HTTP smoke script targets the local Compose API on port 8000. It checks the
identity/wallet/ownership/logout flow and leaves two dummy users and one zero-balance
wallet; its sessions are revoked. Run it only against the development stack.

See [Milestone 1 verification](docs/milestone-1-verification.md) for commands,
observed results, independent review and remaining limitations.
See [Milestone 2 verification](docs/milestone-2-verification.md) for M2 evidence.
