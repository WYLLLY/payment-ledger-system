# Payment & Ledger System — portfolio case study

## Project overview

A Python/FastAPI backend for internal transfers and asynchronous outgoing payments,
built around an immutable PostgreSQL double-entry ledger. The project focuses on
correctness when requests overlap, processes crash and an external provider's outcome
is temporarily unknown. It uses fictional funds in EUR, USD and JPY, without FX.

All six approved milestones are implemented and merged. The maintained suite has
**317 passing tests: 31 unit and 286 integration**, with no failed or skipped tests
on the [verified merged-main run](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38442168726).
This is a completed portfolio project, with explicit limits rather than a claim of
production payment-service certification.

## Engineering problem

An HTTP timeout cannot establish whether a payment happened. Retrying with a new
identity can pay twice; immediately releasing reserved money can allow a second spend
after the provider has already accepted the first payment. Database transactions
cannot atomically commit a remote HTTP response.

The implementation separates local financial correctness from external recovery.
PostgreSQL protects the ledger and balances. Durable outbox work, stable provider
identity, fenced workers and reconciliation resolve the external boundary.

## Design decisions and evidence

| Decision | Purpose | Evidence in the repository |
| --- | --- | --- |
| Integer minor units and currency-specific accounts | Avoid floating-point money and implicit FX. | Bounds, currency and journal-pair tests. |
| Immutable two-entry journals with deferred constraints | Reject incomplete/unbalanced financial operations at commit. | Real PostgreSQL constraint, mutation and rollback tests. |
| Restricted posting functions and separate runtime roles | Prevent direct balance/history writes from API and worker. | Allowed/forbidden role tests and safe function search paths. |
| Transactional retained idempotency | Preserve original results across duplicates and lost responses. | Concurrent same-key/conflicting-payload and replay tests. |
| Deterministic account locking | Prevent double spending and inconsistent lock order. | Competing debits, crossed transfers and local retry tests. |
| Atomic reservation plus outbox | Commit both the funding hold and durable work before HTTP. | Creation fault injection and crash/recovery tests. |
| Persistent provider identity and lease generations | Recover after remote success and reject stale finalization. | Provider restart, duplicate requests, expired leases and worker failures. |
| `needs_review` with retained reserves | Keep uncertain outcomes from becoming false failures. | Retry exhaustion and later definitive outcome tests. |
| Read-only reconciliation and audited operator commands | Investigate safely without rewriting financial history. | Corruption detection, role denials and interrupted recovery tests. |

The [architecture reference](architecture.md) explains transaction boundaries and
privileges. The [review archive](../reviews/README.md) distinguishes maintained tests
from historical independent probes. Passing tests support these mechanisms; they do
not replace the database constraints and transaction design.

## Deliberate scope

One application provides the API, worker and operator CLI. PostgreSQL is the financial
source of truth. A separately persistent SQLite database belongs only to the HTTP
provider simulator. Redis, Kafka and distributed transactions are unnecessary for
this scope. SQLAlchemy 2, Alembic, Docker Compose, Ruff, mypy and GitHub Actions support
the development and verification workflow.

Unknown outcomes keep their reservation, even after bounded retries are exhausted.
There is no force-release command. Provider timeouts bound socket inactivity rather
than total response duration. External reconciliation is paginated observation, and
operators share a restricted database login with incident-ticket attribution.
No real banking integration, refund/FX workflow, production-load certification,
backup/restore qualification or dependency vulnerability audit is claimed.

## Demonstration guide

Use a dedicated local development environment and the [README setup](../README.md).
The [operations runbook](operations-runbook.md) describes operator access and recovery.

1. Start the Compose stack and show successful readiness plus the OpenAPI UI.
2. Explain the two accounts per wallet and fixed dev/test funding boundary.
3. Run the identity/transfer smoke flow; explain retained idempotency and atomic posting.
4. Run the payment smoke flow; show reservation, worker execution and one terminal result.
5. Inspect internal/external reconciliation and pending/needs-review/outbox-age metrics.
6. Walk through the deterministic failure tests and documented unknown-outcome recovery.

```sh
uv run --frozen python scripts/smoke_api.py
PAYMENT_LEDGER_DEV_FIXTURES=1 uv run --frozen python -m scripts.smoke_payments
```

These commands create fictional users and immutable financial history in the local
stack. Funding is explicitly enabled for development; it is absent from normal runtime
permissions. Never use the demo scripts against a shared demonstration deployment.

## Short portfolio description

> Built a Python/FastAPI payment backend with an immutable PostgreSQL double-entry
> ledger, idempotent transfers and asynchronous payments. Implemented transactional
> reservation/outbox handling, worker leases, persistent provider idempotency and
> recovery for unknown outcomes. Verified financial constraints, privileges,
> concurrency, crash recovery and reconciliation with 317 automated tests and CI.
> Includes architecture documentation, independent reviews and operator runbooks.

Use this description with the project title and stack. Repository access is required
while the code is private; do not label its link as publicly accessible source or claim
a public live deployment. The [final verification record](final-verification.md) is
the evidence reference for test and readiness claims.
