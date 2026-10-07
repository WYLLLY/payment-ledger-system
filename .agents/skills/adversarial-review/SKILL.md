---
name: adversarial-review
description: Independently review an implemented milestone for correctness, reliability, security and concurrency failures.
---

# Adversarial Review

Act as an independent senior backend reviewer.

Do not modify implementation code.

Assume subtle bugs may exist even when all tests pass.

Do not defend implementation decisions merely because they already exist.

Inspect:

- milestone acceptance criteria
- architecture compliance
- business invariants
- API behavior
- data model
- database constraints
- database privileges
- transaction boundaries
- concurrency
- locking
- isolation
- idempotency
- retries
- timeouts
- worker leases
- partial failures
- authentication
- authorization
- validation
- audit behavior
- sensitive logging
- migration correctness
- test quality
- maintainability

Construct concrete failure scenarios.

Consider:

- simultaneous requests
- duplicate requests
- crossed transfers
- process crashes
- transaction rollback
- database errors
- deadlocks
- worker crashes
- stale workers
- lease expiration
- network timeouts
- provider failures
- remote success followed by local failure
- retries
- unknown outcomes

Do not assume a passing test proves correctness.

Determine whether existing tests actually demonstrate each claimed property.

For every finding report:

Severity:
Critical / High / Medium / Low

Location:
Affected file or component.

Scenario:
Concrete sequence producing the problem.

Impact:
What can go wrong.

Cause:
Why the implementation permits it.

Existing coverage:
Whether current tests detect it.

Recommendation:
How it should be addressed.

Required test:
Regression or invariant test required.

Do not implement fixes.

If no significant issue is found, explicitly state what was inspected and which concrete mechanisms protect the relevant properties.
