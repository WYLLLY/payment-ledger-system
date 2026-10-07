---
name: verification
description: Execute repository verification before a milestone can be considered complete.
---

# Verification

Run after implementation and again after confirmed review findings are fixed.

Do not hide failures.

Run repository-configured:

- Ruff linting
- formatting verification
- static type checking
- targeted tests
- complete unit test suite
- relevant API tests
- relevant PostgreSQL integration tests

When financial database behavior changed:

- run ledger/invariant integration tests

When privileges changed:

- run allowed and forbidden role tests

When concurrency behavior changed:

- run deterministic concurrency tests

When API behavior changed:

- run relevant API tests

When worker/provider behavior changed:

- run worker, lease, retry and provider-failure tests

When migrations changed:

- migrate a clean database
- verify required schema objects
- verify required constraints
- verify required privileges
- verify the application works with the migrated schema
- test supported migration upgrade paths

Do not replace PostgreSQL-dependent tests with mocks.

If any required command fails:

- do not report verification as successful
- report the exact command
- report the failure
- determine whether the cause is implementation, test or environment

Do not disable tests, lint rules, constraints or type checking merely to obtain a green result.

Produce:

Verification status: PASS / FAIL

Commands executed.

Test results.

Lint results.

Formatting results.

Type-check results.

Integration-test results.

Concurrency-test results when applicable.

Privilege-test results when applicable.

Migration results when applicable.

Skipped checks and exact reasons.

Remaining known issues.

A milestone cannot be considered verified if a required check failed or was silently skipped.
