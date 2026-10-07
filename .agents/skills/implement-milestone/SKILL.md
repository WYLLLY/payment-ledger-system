---
name: implement-milestone
description: Implement exactly one approved milestone of the Payment & Ledger System.
---

# Implement Milestone

Implement only the requested milestone.

Never begin a later milestone automatically.

Before implementation:

1. Read AGENTS.md.
2. Read the approved architecture.
3. Read the complete requested milestone and acceptance criteria.
4. Inspect relevant existing code.
5. Inspect existing tests and migrations.
6. Identify affected database objects.
7. Identify affected invariants.
8. Identify transaction boundaries.
9. Identify concurrency implications.
10. Identify required security boundaries.

If implementation requires contradicting an approved architectural decision, stop and report the conflict.

Do not silently redesign the system.

During implementation:

- Prefer the smallest correct implementation satisfying the milestone.
- Keep business logic outside HTTP handlers.
- Preserve approved architectural boundaries.
- Use PostgreSQL constraints and privileges where the architecture requires database enforcement.
- Never use binary floating point for money.
- Preserve ledger immutability.
- Preserve double-entry accounting.
- Make financial operations atomic.
- Follow approved lock ordering.
- Consider concurrent execution whenever shared state changes.
- Use Alembic migrations for schema changes.
- Do not introduce unnecessary infrastructure or abstractions.
- Do not perform external HTTP calls inside financial database transactions.

Add appropriate tests.

Include normal behavior, expected failures and important edge cases.

When relevant also include:

- database constraint tests
- database privilege tests
- migration tests
- rollback tests
- duplicate-request tests
- idempotency tests
- concurrency tests
- worker lease tests
- external-provider failure tests
- reconciliation tests

Use real PostgreSQL whenever PostgreSQL semantics form part of the guarantee.

After implementation, use the verification skill.

Report:

- files changed
- database changes
- important implementation decisions
- tests added
- verification commands executed
- results
- remaining risks
- deviations from the approved plan

Stop after the requested milestone.
