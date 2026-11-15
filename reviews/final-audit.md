# Payment & Ledger System — remediation audit

**Date** 2026-11-14
**Base** `72f8df3a26642944ac56120ec40bb3bf60144ed5`
**Verdict** **APPROVE** for the local fictional-money portfolio demonstration

| Severity | Evidence, impact and resolution |
| --- | --- |
| Medium | Outbox polling could read deferred rows. Alembic `0007_outbox_due_cutoff` uses a stable per-statement cutoff for due-row selection. The current-lease predicate, lock order, restricted privileges and terminal settlement contracts remain covered. The plan test uses 2,000 deferred rows and checks the indexed condition. |
| Medium | Metrics performed full reconciliation. The metrics CLI now reads operational counters directly. Explicit reconciliation remains available for ledger invariants. A full-suite failure revealed logging context left behind by a CLI invocation. The entry point now restores its caller's context in a `finally` block. |
| Medium | The provider could outlast the usable lease. The worker reserves finalization time and applies one deadline across lookup and submission. The delayed-provider test verifies that funds remain reserved without a terminal posting. |
| Medium | The previous application image reported 50 High package/advisory pairs. The new Alpine runtime removes installer tooling and pins its base and build images by digest. Scheduled CI records the runtime scan. |
| Low | The lock now uses dnspython 2.9.2 and OpenTelemetry API 1.45.2. The published OpenTelemetry patch fixes an advisory identified during the re-audit. [Upstream release](https://github.com/open-telemetry/opentelemetry-python/releases/tag/v1.45.2) |
| Low | The smoke helper validates and uses the configured `API_PORT`. Compose and scanner inputs use explicit image digests. |

| Check | Result | Evidence |
| --- | --- | --- |
| Maintained suite | PASS | 34 unit and 289 integration cases. 323 passed, 0 failed, 0 skipped. PostgreSQL 17 with migration, role, concurrency, HTTP and fault-path coverage. |
| Docker and E2E | PASS | Final image built and started. Both maintained HTTP smoke scripts passed. A preceding run also exercised the nondefault port and a local Payment-to-Fraud journey. |
| Dependencies and runtime | PASS | Hash-enforced Python advisory lookup returned zero known findings. Trivy 0.76.1, cached 2026-11-13 database, returned zero High or Critical results for the built ARM64 application runtime. |
| Static and workflow | PASS | Ruff lint and format, strict mypy, actionlint and diff validation. |
| Hosted CI, deployment, reference-scale load and disaster recovery | SKIPPED | The patch was verified on isolated local resources. No remote execution or hosted environment was exercised. |

Independent review challenged query/index behavior, the shared provider deadline and supply-chain inputs. The post-review regression checked the latest runtime and dependency patch. The portfolio evidence supports transaction, idempotency, reconciliation and asynchronous settlement claims for simulated money.
