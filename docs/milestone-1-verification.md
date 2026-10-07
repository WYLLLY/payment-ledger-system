# Milestone 1 — implementation and verification

Date: 7 October 2026 (Europe/Paris).

**Verification status: PASS locally.** Hosted GitHub Actions execution remains
unverified because this directory has no Git repository or remote. No remote CI
run is claimed. Milestone 2 has not been started.

## Files created

```text
.dockerignore
.env.example
.github/workflows/ci.yml
.gitignore
.python-version
Dockerfile
README.md
alembic.ini
compose.yaml
docs/approved-plan.md
docs/milestone-1-verification.md
docker/init-db.sh
migrations/env.py
migrations/script.py.mako
migrations/versions/0001_foundation.py
pyproject.toml
scripts/init_env.py
scripts/provision_app.py
src/payment_ledger/__init__.py
src/payment_ledger/config.py
src/payment_ledger/database.py
src/payment_ledger/health.py
src/payment_ledger/main.py
tests/integration/__init__.py
tests/integration/conftest.py
tests/integration/test_foundation.py
tests/unit/conftest.py
tests/unit/test_config.py
tests/unit/test_health.py
uv.lock
```

Existing `AGENTS.md` and all four repository-local skills were read and preserved.
The approved plan was copied verbatim from the user attachment. Local generated
files include ignored `.env` (mode 0600), `.venv/` and `.local/`; no secret values
are included in project source, this report or the Docker build context.

## Python structure, boundaries and SOLID decisions

The `src/payment_ledger` package contains only configuration, database
infrastructure, HTTP health contracts and the application factory. Functions and
modules have focused responsibilities. Route handlers delegate the database
probe. The FastAPI dependency provides the lifecycle-managed engine, while the
factory accepts explicit settings for tests. No repository/service/interface
layers or speculative domain objects were introduced.

This applies single responsibility and dependency injection where useful.
Open/closed, Liskov and interface segregation do not justify artificial class
hierarchies for this milestone. Settings, infrastructure and HTTP concerns remain
separate. No financial state, auth, payment or provider code exists.

## Tools and configuration

- Python 3.14 (host 3.14.6; final container build used 3.14.8).
- uv 0.12.23 and committed `uv.lock`; exact resolved versions are in the lockfile.
- FastAPI, Pydantic Settings, SQLAlchemy 2, psycopg 3 and Alembic.
- Ruff lint/format, strict mypy with Pydantic plugin, pytest and httpx2.
- Multi-stage Docker image, non-root UID 10001, loopback-only published ports.
- Compose PostgreSQL → one-shot migration/provisioning → API startup ordering.
- GitHub Actions workflow with frozen dependencies, unit tests, full Compose
  build, real PostgreSQL tests, HTTP smoke probes and cleanup.

Homebrew installed PostgreSQL 17.11, Docker CLI, Compose, Colima and actionlint
(plus their dependencies, including ShellCheck). The existing machine initially
had no reachable Docker daemon. A separate Colima profile `payment-ledger` was
started with 2 CPUs, 4 GiB RAM and a 20 GiB disk; the default context was not
changed. The Compose PostgreSQL image is version 17. The host Homebrew PostgreSQL
service was not started because the verified server runs inside Compose.

## Database and migration work

Administrator initialization provisions the non-superuser `payment_migrator`
login with CREATEROLE and database ownership. Alembic then creates restricted
runtime roles and the empty `payment` schema, revokes PUBLIC schema/database
privileges and grants runtime CONNECT/USAGE only. New migrator-owned functions
are not executable by PUBLIC. The migration checks dangerous attributes and
memberships on reused roles and refuses a LOGIN-enabled worker role.

Application password provisioning is separate from migrations and uses a
client-generated SCRAM verifier. Only the migration service receives migration
credentials. The runtime username is fixed to `payment_app`; its SQLAlchemy
isolation level is explicitly READ COMMITTED. The worker role remains NOLOGIN.

PostgreSQL DDL runs within the Alembic transaction. There are no financial
transactions, ledger objects or lock ordering to implement at this stage.
Migration execution must be serialized. Schema downgrade does not use CASCADE;
cluster-wide roles and restrictive security defaults intentionally survive.

This is the first revision: the predecessor is `base`. Tests exercised a fresh
database → `0001_foundation`, a repeat upgrade to head and head → base → head.
There is no previous numbered revision to test.

## Tests created and observed results

**43 passed in 19.65 seconds, no warnings and no skips:**

- 8 unit/API contract tests: required/redacted password, special characters in
  URLs, port validation, typed environment, fixed application identity, liveness
  without DB and OpenAPI health contract.
- 35 integration cases using real PostgreSQL: fresh schema/version/owner,
  base/head lifecycle, actual application login, isolation and statement timeout,
  successful readiness, failed authentication without secret leakage, schema
  disappearance/recovery with liveness preserved, runtime role attributes and
  membership, denied DDL/TEMP/role escalation/migration-table access, and private
  default SELECT/INSERT/UPDATE/DELETE/TRUNCATE/function execution privileges.
- API privileges are tested through actual password authentication. Worker
  privileges use an administrator connection that reduces session authorization
  to the NOLOGIN worker role before attempts. Tests create unique databases and
  remove them during cleanup.

The actual Dockerized API returned HTTP 200 and `{"status":"ok"}` from both
health endpoints. Compose reported PostgreSQL and API healthy, with the migration
service exiting successfully. Container assertions confirmed UID 10001, no
administrator/migration password environment variables and no `/app/.env`.

## Exact verification commands executed

Executed from `/Users/william/Documents/Projet 1`:

```sh
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest tests/unit
.venv/bin/pytest tests/integration
.venv/bin/pytest
PATH=/opt/homebrew/bin:$PATH actionlint .github/workflows/ci.yml
PATH=/opt/homebrew/bin:$PATH shellcheck docker/init-db.sh
UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline
DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose config --quiet
DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180
DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose config --quiet
DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180 > .local/compose-verification.log 2>&1
curl --fail --silent http://127.0.0.1:8000/health/live
curl --fail --silent http://127.0.0.1:8000/health/ready
DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose ps
DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose exec -T api python -c 'import os; from pathlib import Path; assert os.getuid() == 10001; assert "MIGRATION_DB_PASSWORD" not in os.environ; assert "POSTGRES_PASSWORD" not in os.environ; assert not Path("/app/.env").exists(); print("Runtime isolation: PASS")'
```

Integration fixtures invoke these commands with the test database name and
migrator credentials supplied through the environment (never printed):

```sh
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m alembic downgrade base
.venv/bin/python -m alembic upgrade head
```

The executable used by those subprocesses is the absolute `sys.executable`
inside `.venv`. Docker's migration service runs `alembic upgrade head` followed by
`python scripts/provision_app.py`.

Results: Ruff passes; 22 Python files considered formatted; mypy passes for 15
configured source files; actionlint and ShellCheck exit 0; the lockfile check
resolves 40 packages; Compose configuration/build/startup passes.

## Failures encountered and corrections

- Initial `docker` and PostgreSQL executable discovery failed; required tools
  were installed. Docker CLI subsequently had no daemon, so an isolated Colima
  engine was started.
- Initial sandboxed pip download failed DNS resolution; the authorized network
  retry installed uv and project dependencies successfully.
- The first `ruff check .` traversed `.local/uv-cache` because the directory is
  not a Git checkout. Explicit exclusions now cover only local tools/caches;
  project code remains checked. Source formatting/line-length issues were fixed
  using `.venv/bin/ruff format .`, and one test import was sorted using
  `.venv/bin/ruff check --fix tests/integration/test_foundation.py`.
- Initial tests passed with a Starlette warning about deprecated httpx usage.
  The dev dependency was replaced with the locally installed Starlette version's
  supported httpx2 client. The final suite passes without warnings.
- After review, timeout wording was corrected, worker privilege checks and
  schema recovery coverage were added, and the complete verification ran again.

No test, lint rule, type rule or database constraint was disabled to get green.

## Independent review

A separate reviewer applied `.agents/skills/adversarial-review/SKILL.md`, read
the financial review skill, inspected the complete implementation and ran the
8 unit tests independently. Financial invariants are not applicable to this
empty foundation. No High/Critical or other important code defect was found.

One Low documentation finding: connection, pool and server-side statement
timeouts do not provide an absolute network response deadline after a TCP
connection has been established. The module description and README were corrected
to state that limit. The reviewer validated those corrections, worker tests,
schema recovery test and httpx2 change; a final minor README clarification of
administrator usage in worker tests was also applied.

The reviewer's attempted isolated network-stall reproduction was blocked at
socket creation by sandbox permissions. No claim is made that this scenario has
been experimentally verified. No hard end-to-end network deadline is promised.

## Skipped checks, limitations and deviations

- **Hosted GitHub Actions:** not executed; there is no initialized Git repository,
  remote or pushed workflow. The YAML passes actionlint and the substantive
  commands ran locally with real containers/PostgreSQL. An actual hosted run
  remains necessary after publishing the repository.
- **Previous numbered migration:** not applicable; this is revision 0001.
  The actual predecessor (`base`) was tested.
- **Financial, idempotency, worker/provider and financial concurrency suites:**
  not applicable; these features are explicitly deferred by the approved plan.
- **Network blackhole deadline:** not guaranteed; see the reviewed limitation
  above. Compose applies a separate process timeout to its own health probe.
- **Deployment scope:** local development/CI only. Database role names are
  cluster-wide, migrations are serialized, and initialization/password rotation
  assumptions are documented in README. Runtime container tags follow Python
  3.14 and PostgreSQL 17 patches; Python packages are locked.
- **Architecture:** no deviation from the approved milestone. Administrator
  provisioning is the necessary prerequisite for Alembic to authenticate as a
  distinct non-superuser migration role. No later business feature was added.

The local stack is left running at `http://127.0.0.1:8000` for inspection. Stop it
with `DOCKER_CONTEXT=colima-payment-ledger docker-compose down` if desired.

Relevant implementation references:
[PostgreSQL cluster-wide role semantics](https://www.postgresql.org/docs/17/sql-createrole.html)
and [Compose startup dependencies](https://docs.docker.com/compose/how-tos/startup-order/).
