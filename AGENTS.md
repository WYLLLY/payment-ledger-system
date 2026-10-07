# Payment & Ledger System — Agent Instructions

## Project Goal

This repository is a production-oriented backend portfolio project built with Python, FastAPI, PostgreSQL, SQLAlchemy 2 and Alembic.

The system implements internal transfers and asynchronous outgoing payments using an immutable double-entry ledger.

Supported currencies are EUR, USD and JPY without currency conversion.

Monetary values use integer minor units.

This is not a Stripe clone.

The objective is to demonstrate backend engineering fundamentals:
API design, PostgreSQL, relational modeling, transactions, financial invariants, concurrency, idempotency, security, testing, observability and reliability.

Prefer correctness, explicitness, simplicity and testability over unnecessary complexity.

Do not introduce microservices, Redis, Kafka, distributed tracing, additional databases or other infrastructure unless an approved architectural change demonstrates a concrete requirement.

## Source of Truth

Before modifying code:

1. Read this AGENTS.md.
2. Read the approved architecture and current milestone.
3. Inspect the existing implementation, tests and migrations.
4. Respect previously approved architectural decisions.

The approved Payment & Ledger System plan is the architectural source of truth.

Do not silently change architecture.

If an approved architectural decision appears incorrect or impossible to implement safely, stop and report the issue before changing it.

Implement only the currently requested milestone.

Never automatically start the next milestone.

## Financial Correctness

Financial correctness has priority over convenience or performance.

Money must never be silently:

- created
- destroyed
- duplicated
- double-spent

Never use binary floating-point types for monetary values.

Amounts are represented as integer minor units and must respect configured bounds.

Ledger entries are immutable after creation.

Every journal must contain exactly two entries of the same currency with opposite amounts.

Every completed journal must therefore remain balanced.

User financial accounts must never become negative.

Only explicitly approved system accounts may have negative balances.

Critical financial invariants must be enforced by PostgreSQL whenever reasonably possible.

Application-level validation does not replace database constraints.

## Ledger

Each wallet has exactly one `available` and one `reserved` ledger account.

Ledger writes must go through the approved PostgreSQL posting mechanism.

The application and worker roles must not directly modify ledger entries or projected balances.

Ledger UPDATE, DELETE and unauthorized TRUNCATE operations must remain prohibited.

Projected balances and ledger entries must change atomically.

The ledger is the authoritative financial history.

Reconciliation may detect inconsistencies but must never silently rewrite financial history.

## Transactions

Financial operations must have explicit transaction boundaries.

A financial operation must never expose partially committed internal financial state.

Transfers must atomically combine the relevant idempotency operation, business operation, ledger posting, audit event and stored response.

Payment creation must atomically combine the relevant idempotency operation, payment creation, reservation, outbox task, audit event and stored response.

Payment finalization must atomically combine the terminal ledger posting and terminal payment state.

No external HTTP request may occur inside these database transactions.

A transaction that has committed must not be blindly replayed.

## Concurrency

Assume requests and workers execute concurrently.

The approved default transaction isolation level is READ COMMITTED.

Financial operations must follow the approved deterministic locking strategy.

When multiple ledger accounts require locking, acquire them in ascending identifier order.

Consider:

- simultaneous transfers
- simultaneous withdrawals
- crossed transfers
- duplicate requests
- concurrent idempotency keys
- concurrent workers
- lease expiration
- stale workers
- lost updates
- write skew
- deadlocks

Retryable local transaction failures may trigger a bounded retry of the entire local transaction.

Never retry an already committed financial operation without passing through its idempotency mechanism.

Concurrency guarantees must be demonstrated using deterministic synchronization where practical rather than timing-based sleeps.

## Idempotency

POST /transfers and POST /payments require an Idempotency-Key.

Uniqueness is scoped by:

(user_id, operation_kind, key)

The canonical request payload must be hashed.

The same key with the same canonical request must return the original operation/result semantics.

The same key with a different request must be rejected with a conflict.

Idempotency must remain correct under concurrent duplicate requests.

Do not implement idempotency using an unprotected check-then-insert sequence.

Database uniqueness and transactional behavior must protect the race.

Financial idempotency records are retained according to the approved architecture.

## External Payment Provider

The external provider is simulated but must be treated as unreliable.

Expect:

- latency
- timeout
- temporary failure
- duplicate requests
- retries
- worker crashes
- unknown outcomes
- remote success followed by local failure

Provider operations use a stable idempotency key derived from the immutable payment identity.

Never generate a new provider key for a retry of the same payment.

A timeout does not mean failure.

An unknown remote outcome must not cause reserved funds to be released.

After an unknown outcome, query provider state before making further decisions.

Retries must be bounded and use the same provider identity.

After retry exhaustion, the payment enters `needs_review` and funds remain reserved.

Reconciliation may later resolve the state but must not infer failure merely from temporary lack of response.

## Worker and Outbox

Payment creation and external execution are separated by the transactional outbox.

Workers claim eligible work using the approved PostgreSQL locking strategy.

External HTTP calls occur after the claiming transaction commits.

Worker leases and generations must prevent stale workers from overwriting newer decisions.

A worker must verify payment state and lease generation before finalization.

Multiple workers must not produce duplicate terminal financial effects.

## Payment State Machine

Payment states are:

- pending
- needs_review
- succeeded
- failed

`succeeded` and `failed` are terminal.

A payment has at most one reservation journal.

A payment has exactly one terminal financial outcome when finalized:

- settlement
or
- release

Never both.

The database must enforce terminal journal exclusivity where specified by the approved architecture.

## Security

Never commit credentials, tokens, passwords or secrets.

Passwords use the approved Argon2id strategy.

Sessions are opaque and stored using the approved hashed-token design.

Authentication and authorization are separate concerns.

Resource ownership must be enforced.

Unauthorized access to another user's account should follow the approved not-found behavior.

Financial PostgreSQL functions must validate account ownership and other trusted parameters rather than relying exclusively on HTTP-layer authorization.

Migration credentials must never be used by the running API or worker.

SECURITY DEFINER functions must use a safe search_path and minimal execution privileges.

Do not expose sensitive internal information through API responses or logs.

## Database Privileges

Database privileges are part of the security and correctness model.

Application and worker roles receive only required privileges.

Migration privileges remain separate.

Dev/test funding capabilities must not exist in the demonstration deployment role.

Tests must verify both allowed and forbidden database operations where privileges form part of a guarantee.

## Errors and Reliability

Expected failures require explicit behavior.

Do not silently swallow exceptions.

Retries must be bounded.

External operations require explicit timeouts.

Distinguish retryable failures, permanent failures and unknown outcomes.

Never convert an unknown external outcome into a financial failure merely for convenience.

## Testing

Tests must verify properties and invariants rather than merely exercise lines of code.

Use the appropriate combination of:

- unit tests
- API tests
- PostgreSQL integration tests
- privilege tests
- migration tests
- concurrency tests
- worker/provider failure tests

Use real PostgreSQL whenever correctness depends on:

- transactions
- constraints
- triggers
- privileges
- locking
- isolation
- concurrent behavior

Mocks must not replace tests for these PostgreSQL guarantees.

Concurrency tests should use deterministic barriers or synchronization where practical rather than random sleeps.

Every confirmed bug should receive a regression test when practical.

A passing test suite does not by itself prove financial correctness.

## Observability and Audit

Use structured JSON logs according to the approved milestone.

Never log secrets or raw authentication credentials.

Use correlation identifiers for important request and payment flows.

Audit events associated with successful state transitions must share the appropriate transaction.

Operational metrics should cover meaningful states such as pending payments, needs_review payments and outbox age.

Observability must remain proportionate to this monolithic portfolio project.

Distributed tracing is not required unless the architecture is explicitly changed.

## Agent Workflow

Implementation work must use the `implement-milestone` skill.

After implementation and after corrections, use the `verification` skill.

Independent review must be delegated to a separate reviewer/subagent using the `adversarial-review` skill.

When changes affect financial state, ledger behavior, transactions, concurrency, locking, payments or idempotency, the independent reviewer must additionally use the `financial-invariant-review` skill.

The implementation agent must not treat its own review as an independent review.

Confirmed findings return to the primary implementation agent for correction.

After corrections, verification must run again.

Do not proceed to the next milestone until the current milestone satisfies its acceptance criteria and required verification.

## Definition of Done

A milestone is complete only when:

- its approved acceptance criteria are satisfied
- required migrations exist
- required database constraints and privileges exist
- required tests exist
- relevant tests pass
- linting passes
- formatting verification passes
- type checking passes
- relevant PostgreSQL integration tests pass
- relevant privilege tests pass
- relevant concurrency tests pass
- important independent-review findings are resolved
- no unexplained architectural deviation remains
- documentation is updated where required

Do not declare a milestone complete when required verification has failed or was silently skipped.
