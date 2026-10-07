---
name: financial-invariant-review
description: Review financial correctness for ledger, balances, transfers, payments, transactions, concurrency and idempotency.
---

# Financial Invariant Review

Use whenever changes affect financial state or financial correctness.

Do not modify implementation code.

Identify every financial invariant affected.

Verify that:

- money cannot be silently created
- money cannot be silently destroyed
- money cannot be double-spent
- user balances cannot become negative
- ledger entries remain immutable
- journals remain balanced
- journals contain the required pair of entries
- currencies cannot be mixed inside a journal
- duplicate requests cannot duplicate financial effects
- concurrent operations preserve invariants
- a payment cannot both settle and release its reservation
- unknown provider outcomes cannot incorrectly release funds

For every invariant identify the concrete protection:

- PostgreSQL constraint
- constraint trigger
- database privilege
- transaction
- row lock
- lock ordering
- isolation property
- uniqueness constraint
- application logic
- test

If no concrete mechanism protects an invariant, treat the guarantee as unproven.

Identify exact transaction boundaries.

Consider failures:

- before transaction
- during transaction
- immediately before commit
- immediately after commit
- during response delivery
- before external provider request
- after provider acceptance
- before local finalization

Review concurrency scenarios:

- two transfers from the same account
- crossed transfers
- simultaneous withdrawals
- concurrent duplicate idempotency keys
- concurrent payment creation
- multiple workers
- stale worker after lease expiration
- retry while original execution remains active

Look specifically for:

- lost updates
- write skew
- check-then-act races
- deadlocks
- double spending
- duplicate ledger entries
- duplicate terminal journals
- stale-worker writes

Review API idempotency:

- database uniqueness
- concurrent duplicates
- canonical request hashing
- same key with different payload
- response persistence
- retries after commit

Review provider idempotency:

- stable provider key
- parameter consistency
- timeout before execution
- timeout after execution
- provider success followed by local failure
- duplicate provider request
- retry after unknown outcome

Verify that an unknown provider outcome leaves funds reserved.

Require concrete tests for important financial and concurrency guarantees.
