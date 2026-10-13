# Final verification and repository status

Date: 13 October 2026 (Europe/Paris).

This finalization adds no business feature and does not merge a pull request. It consolidates
English documentation and verifies the complete system from an isolated installation.
The clean-install smoke uncovered a dev/test provisioning defect; the small correction and
its real PostgreSQL regression are described below. The approved architecture is unchanged.

## Integration and approval

| Milestone | Status verified against Git/GitHub |
| --- | --- |
| M1 foundation/CI | In main (`0edadff`); included in complete verification. |
| M2 identity/wallets | In main (`c47f027`); included in complete verification. |
| M3 ledger | PR #1 merged (`fbc62d1`); included in complete verification. |
| M4 transfers | PR #2 merged (`eb42cce`); included in complete verification. |
| M5 payments/recovery | PR #3 merged (`0df1832`); included in complete verification. |
| M6 resilience/operations | User approved; implemented on `feature/m6-resilience`, PR #4 OPEN, targeting main. Not merged. |

Main remains `0df1832c6a2f4e42d7be31a3867b0b6b9393eb77`. M6 implementation is
`abbf52a` and its first verification/runbook commit is `deb99fb`. The finalization commits
follow those on the same branch; the clean-install correction is `2558dce`. No history is rewritten. See
[PR #4](https://github.com/WYLLLY/payment-ledger-system/pull/4) for the current head and CI.
“All milestones implemented and validated” does not mean M6 has already been integrated
into main. Explicit merge authorization remains required.

## Isolated installation

A clean clone of the committed M6 revision was created without copying the existing `.env`,
virtual environment or data. `uv sync --frozen` installed the lockfile into a new venv using
Python 3.14.6. `scripts/init_env.py` generated four independent fresh secrets in mode 0600.
The final verification includes the fixture correction below, copied identically into that
checkout before testing. Existing host toolchains and dependency/image caches were reused;
this is a clean application/data installation, not a newly installed operating system.

A separate Compose project `payment-ledger-final-20261013` used fresh PostgreSQL/provider
volumes and ports 25432, 18000 and 18001. The normal development project's volumes and
credentials were not changed. PostgreSQL initialization, all six migrations, credential
provisioning and API/provider startup succeeded; the worker executed an actual payment.
The worker has no application healthcheck, so its successful execution is evidence at test time.

The first attempt under `/private/tmp` failed because the path was not correctly shared
with the Colima VM: the init script appeared as a directory and PostgreSQL exited 126.
The installation was recreated under the shared home directory. This environment failure
was recorded, not counted as successful verification. Only disposable verification volumes
were removed/recreated. For Colima, place the checkout in a directory shared with its VM.

Key commands (run in the isolated checkout with `DOCKER_CONTEXT=colima-payment-ledger`):

```sh
uv sync --frozen
python scripts/init_env.py
# Configure the isolated ports in this new .env without exposing its generated secrets.
docker-compose -p payment-ledger-final-20261013 up --build --wait --wait-timeout 180
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pytest -q --tb=short --junitxml=../clean-tests-final.xml
```

Both unchanged smoke scripts were executed through a temporary Python harness that maps
only their hard-coded API URL from port 8000 to isolated port 18000. Database settings came
from the isolated `.env`; the payment smoke explicitly opted into dev fixtures. Requests
still traversed real HTTP, API, PostgreSQL, worker and persistent provider services. No
mock replaced PostgreSQL, transactions or provider network behavior.

## Clean-install defect and correction

Before correction, the standalone payment smoke failed on `SET LOCAL ROLE payment_dev`
with `InsufficientPrivilege`. `provision_dev_role` created/validated the fixture role but
did not guarantee the migrator's SET membership. The main seed command and some earlier
tests supplied that grant separately, masking the missing prerequisite in other callers.
The initial complete suite still passed all 316 existing tests; that did not invalidate
the separately observed clean-install failure.

The existing `GRANT payment_dev TO payment_migrator WITH SET TRUE` now belongs to the
shared dev/test provisioning helper, after its role/membership safety checks. The redundant
grant was removed from `seed_demo.main`. The fixed recipient is the migrator, not API or
worker; normal migrations/Compose startup still do not enable fixture funding. No financial
function, schema, runtime behavior or new feature was added.

`test_fixture_provisioning_restores_set_without_runtime_funding` explicitly removes SET
from the real existing memberships, proves role switching fails, provisions again, posts
one fixed opening as the dev role and verifies its balance, ledger consistency and absence
of API/worker funding execution rights. The targeted regression passed. Then the temporary
volumes were recreated and **both smoke scripts passed before any pytest test ran**, proving
the fix no longer relies on earlier test side effects.

The separate reviewer approved the privilege correction using adversarial/financial-invariant
review. No additional runtime permissions or unresolved correction finding was identified.
The maintained suite now has one additional regression: **317 tests**, not 316.

## Verification results

Verification status: **PASS**. Final complete verification results are recorded below. Historical M1–M6
reports retain their original counts; temporary independent review experiments are not
included in the maintained-suite total.

| Check | Result |
| --- | --- |
| Complete M1–M6 pytest suite after correction | **317 passed, 0 failed, 0 errors, 0 skipped** in 104.45 s |
| Unit/integration breakdown | 31 unit + 286 integration |
| Ruff lint and formatting | PASS |
| Strict mypy | PASS, 59 source files |
| actionlint / shellcheck | PASS |
| Clean Compose startup and migrations | PASS |
| Real HTTP identity/transfer smoke | PASS |
| Real HTTP reservation/worker/provider/settlement/replay/reconciliation smoke | PASS, before any tests on fresh volumes |
| Targeted missing-SET regression | PASS |
| Independent documentation review | APPROVE; worker schema USAGE vs CREATE wording corrected |
| Independent privilege-fix review | APPROVE |

The complete suite covers all six milestones, including PostgreSQL constraints, forbidden
privileges, migration paths, concurrency, crashes, reconciliation and recovery. No required
check is intentionally skipped. The final CI on PR #4 must complete separately from these
local results; the earlier M6 head's two runs were already SUCCESS:
[push](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38269073129) and
[PR](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38269152381).

## Git secret checks

Gitleaks 8.30.1 was downloaded from the official GitHub release; its archive SHA-256 matched
the published checksum. It ran locally with full redaction, scanning Git history using
`--log-opts="--all"`, and a tracked-source export including the documentation updates.
No allowlist was added to suppress findings. An additional exact-match check of every local
Git blob against the existing environment's actual password values found no matches
(165 blobs before finalization commits). Only counts/metadata were emitted, never passwords.
Tracked environment-file history contains `.env.example`, not `.env` or private key files.

These checks report **no detected committed secret**, not a mathematical guarantee that
arbitrary undisclosed secrets cannot exist. They are not a dependency vulnerability audit.
Generated secrets, logs, scanner binaries and clean-checkout artifacts remain outside tracked
source in ignored verification directories. Final scans are repeated after committing.

## Documentation, scope and remaining limitations

The README is finalized in English, with runnable setup, API contracts, architecture,
verification, operations and explicit scope limits. `docs/architecture.md` gives an English
reference for database guarantees, transactions, role boundaries and state transitions.
The original approved plan and historical verification reports are preserved. The operations
runbook remains the canonical incident/recovery procedure.

Remaining limits are intentional or independently reported, not hidden implementation claims:

- Fictional funds/provider only; no real banking integration, refunds, FX or callbacks.
- Provider timeout is socket inactivity, not a hard total-response deadline; slow streams
  can delay processing. Fencing/idempotency still protect terminal effects.
- Unknown outcomes after exhaustion stay reserved; no forced release or retry-budget reset.
- External reconciliation is paginated observation, not a distributed atomic snapshot.
- Shared operator login uses incident-ticket attribution; runtime/migrator principals are
  trusted boundaries. Fixed-window quotas allow boundary bursts.
- No automatic retention cleanup, backup/restore qualification, host-power/disk-loss testing,
  production-load certification or dependency vulnerability audit is claimed.
- Some additional adversarial experiments described in local independent review reports
  are not permanent CI regression tests. No new feature was added to address those gaps.

Existing local changes in `reviews/M4-review.md`, `reviews/M5-review.md` and the untracked
`reviews/M6-final-review.md` belong to prior review work. They were read where relevant,
preserved without modification and excluded from finalization commits. The checkout therefore
is not reported as globally clean; the delivered changes are committed separately.
