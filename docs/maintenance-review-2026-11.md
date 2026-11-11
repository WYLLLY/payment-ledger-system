# November 2026 maintenance report

The CI workflow now pins checkout and Python setup to the commits behind their official v4 and v5 tags at the time of review. The pins give each run an immutable reference for the same action versions.

The verification job still runs lint, formatting, type checks, unit and integration tests on PostgreSQL, and HTTP and payment simulator smoke tests. See the pull request checks for the results on this change.
