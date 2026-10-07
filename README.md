# Payment & Ledger System

Milestone 1: a typed FastAPI application, real PostgreSQL, restricted database
roles, Alembic migrations and CI. No financial or authentication functionality is
implemented yet. The [approved plan](docs/approved-plan.md) defines later work.

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
uv run --frozen uvicorn payment_ledger.main:create_app --factory
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
migrations/       versioned schema and privilege changes
docker/           administrator bootstrap for a fresh PostgreSQL volume
scripts/          local secret generation and runtime credential provisioning
tests/unit/       configuration and HTTP contracts without PostgreSQL
tests/integration/ real PostgreSQL, role privileges and migration lifecycle
```

Modules have focused responsibilities. The engine is injected through FastAPI's
request dependency and disposed at application shutdown. There are no speculative
repositories, service layers or interfaces. This is the concrete use of single
responsibility and dependency injection here; no class hierarchy is needed.

`GET /health/live` reports process liveness without a database connection.
`GET /health/ready` checks connectivity as `payment_app` and schema access. It
returns 200 with `{"status":"ok"}` or a generic 503 with
`{"status":"unavailable"}`. Errors are not echoed to clients or logs. Readiness
configures connect, server-side statement and pool wait timeouts. These are not an
absolute network deadline: an established TCP connection with a nonresponsive
peer can outlast them. The container probe has its own process timeout. It checks the Milestone 1
schema, not compatibility with hypothetical future revisions.

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
- Runtime roles get database CONNECT and schema USAGE only. They receive no
  table writes, schema creation, TEMP privileges or migration-table access.
  New functions created by the migrator are not executable by PUBLIC.
  `payment_worker` remains NOLOGIN until a later approved milestone needs it.
- Downgrading to `base` removes the application schema and its runtime CONNECT
  grants. It intentionally retains cluster-wide roles and restrictive defaults;
  deleting them could affect another database. No `CASCADE` is used.
- This is the first revision. The previous state is Alembic `base`, with no
  earlier versioned schema. Tests cover fresh database → head, repeated head,
  head → base → head. Future revisions must add their actual predecessor upgrade
  checks.

Run migrations with `uv run --frozen alembic upgrade head`. Migration execution is
online only because role and database validation requires PostgreSQL. Application
startup never applies migrations. Deployment must serialize migrations; concurrent
migration runners are outside the supported workflow.

## Verification

```sh
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen mypy
uv run --frozen pytest tests/unit
docker compose up --build --wait --wait-timeout 180
uv run --frozen pytest tests/integration
uv run --frozen pytest
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

See [Milestone 1 verification](docs/milestone-1-verification.md) for commands,
observed results, independent review and remaining limitations.
