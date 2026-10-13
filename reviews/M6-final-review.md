# M6 Independent Review and Final M1–M6 Audit

Date: 13 October 2026 (Europe/Paris).

**Final verdict: APPROVE. Required verification: PASS.**

**Repository suite: 316 PASS / 0 FAIL / 0 ERROR / 0 SKIPPED.**
**Additional review suite: 12 PASS / 0 FAIL / 0 ERROR / 0 SKIPPED**, including
one complete real-HTTP Compose E2E journey. Total pytest cases: **328 PASS**.
Two separate manual checks validated live log privacy/correlation and reproduced
the documented socket-timeout limitation; they are not counted as pytest cases.

No Critical, High or Medium finding was identified. Two Low, nonblocking findings
remain: the documented absence of an absolute provider-response deadline, and
additional regression coverage that currently exists only as review probes.
No financial invariant failure, unsafe operator action or M1–M5 regression was
observed. This verdict authorizes no merge.

## Reviewed revision, scope and independence

- PR: [#4 — feat: add resilience and operator recovery (M6)](https://github.com/WYLLLY/payment-ledger-system/pull/4).
- Branch: `feature/m6-resilience`.
- Exact reviewed head: `deb99fba6a8c85c4aaf657f33e55cc7a00b0741d`.
- Base: `main`, `0df1832c6a2f4e42d7be31a3867b0b6b9393eb77`, containing merged M1–M5.
- Local HEAD and live PR head matched at initial inspection and again before
  this report. PR remained OPEN with merge state CLEAN; no GitHub approval or
  merge was submitted.
- Read current `AGENTS.md`, `docs/approved-plan.md`, `adversarial-review`,
  `financial-invariant-review`, `verification`, and repository Git workflow guidance.
- Reviewed M6 migration, rate admission, reconciliation, operator tooling,
  observability, runbook and tests, plus inherited M1–M5 identity, financial
  services, migrations, constraints, privileges, worker/provider and deployment.
- Two separate subagents independently examined (1) financial/DB/concurrency
  guarantees and (2) operations/logging/reconciliation/Docker/CI. Their conclusions
  were checked against the primary review and actual verification results.
  The financial reviewer also inspected the additional E2E/adversarial test source.
  Earlier implementation verification reports were background, not substitutes
  for executing the complete suite in this review.
- No implementation or repository test source changed. Only this new report was
  added. Existing changes in `reviews/M4-review.md` and `reviews/M5-review.md`
  were preserved; their SHA-256 hashes matched before and after review.
  No commit, push, merge, or repair was performed.

## Findings

| Severity | Count | Classification |
| --- | ---: | --- |
| Critical | 0 | No confirmed finding |
| High | 0 | No confirmed finding |
| Medium | 0 | No confirmed finding |
| Low | 2 | Documented availability limit; retained-test coverage gap |

### L1 — Provider socket timeout does not bound total response duration

**Severity:** Low, nonblocking; pre-existing and explicitly documented.

**Location:** `src/payment_ledger/payments/provider.py:63–66`, called by
`payments/worker.py:121–127` and `operations/service.py:164`.
`docs/operations-runbook.md:142–145` already explains the limitation.

**Scenario:** The provider sends response chunks frequently enough to avoid the
socket inactivity timeout while extending the overall response. The serial worker
continues awaiting network I/O beyond its lease; operator reconciliation can also
wait longer than the configured timeout suggests.

**Impact:** Reduced worker/reconciliation availability and delayed recovery.
Reservation retention, stable provider identity and generation checks still protect
financial state. This is not evidence of double payment or unsafe release.

**Cause:** `urlopen(..., timeout=...)` applies to socket inactivity; no absolute
elapsed-time deadline cancels and closes the underlying request.

**Executed evidence:** A separate reviewer ran a bounded real-TCP probe serving a
valid matching result in 12 chunks at 80 ms intervals. With `timeout=0.2` seconds,
lookup returned `succeeded` after **0.920 seconds**, **4.60 times** the configured
timeout. The probe asserted a matching result, duration above three timeouts and
completion below two seconds. No database or repository source was modified.

**Existing coverage:** Repository tests cover timeout before/after provider
acceptance, 503, protocol errors and invalid evidence. They do not enforce a total
response deadline. The finite probe confirms the disclosed behavior; it is not a
failing test of an implemented total-deadline contract.

**Recommendation:** Add an explicit total request deadline when hardening service
availability. Cancellation must close the transport; merely timing out a wrapper
around an ongoing thread is insufficient. Preserve `unknown` and reserved funds.

**Required regression for that improvement:** A controlled slow-stream provider
must exceed the total deadline while staying below each inactivity timeout. Assert
bounded return, closed transport, retained reservation, continued worker processing,
and safe late-result recovery.

### L2 — Important composed scenarios are not retained in repository CI

**Severity:** Low, nonblocking coverage finding; no current behavior failure.

**Location:** `tests/integration/test_payments.py`,
`tests/integration/test_resilience.py`, and the CI E2E smoke coverage.

**Scenario:** A future change breaks populated M5→M6 migration, USD/JPY payment
finalization, overlapping old/new workers during provider execution, release
rollback, or response-loss replay. Existing tests establish many component
protections separately, but do not retain all the composed cases executed here.

**Impact:** Less direct protection against future regressions in those combinations.

**Cause:** The additional review cases were kept outside the repository to respect
the request not to modify code.

**Existing coverage:** Eleven additional adversarial cases and one complete Compose
E2E passed in this review. The E2E drops both financial responses only after the
upstream API has committed, then checks exact replay and one financial effect.
The populated upgrade probe preserves all four payment states and existing history.

**Recommendation:** Promote these cases into maintained tests and retain the funded
transfer→payment→internal/external-reconciliation E2E in CI in a separately
authorized change. No implementation correction is required by their results.

**Required regression content:** Preserve the scenario matrix and assertions in
“Additional independent tests” below, especially database state, journal counts,
fenced generations and rollback assertions rather than only HTTP status codes.

## M6 assessment

| Area | Mechanisms inspected and executed evidence |
| --- | --- |
| Resilience and recovery | Existing bounded local retries, five-claim provider budget, lookup-before-submit, persisted provider identity, expired leases and generation fencing remain intact. Actual worker kill after provider commit, terminated PostgreSQL backend before local commit, operator interruption, stale workers and exhausted reservations all pass. |
| Internal reconciliation | `operations/service.py:41–95` combines ledger/projection checks, pair integrity, currency conservation, payment journal/account/amount matching, outbox agreement, reserved-balance comparison and metrics in one SQL statement/snapshot. Corruption probes verify detection without repair. |
| External reconciliation | `operations/service.py:144–191` uses UUID keyset pagination, validated provider identity, no DB connection held across HTTP, and state/updated_at recheck to classify concurrent progress. Unknown/absent results do not release funds. Contradiction, unavailable provider, unresolved reservation and awaiting-finalization paths are distinguished. |
| JSON logs and correlation | Request middleware accepts UUID request IDs or creates one, returns the ID, logs route templates and allowlisted fields, suppresses exception text and resets context. Transaction-local correlation links API financial audit to request ID; worker/provider use payment UUID. Live E2E logs and audit assertions passed. |
| Metrics | Pending, needs_review, oldest unfinished outbox age and expired leases are available through the restricted operator interface. Empty, pending, expired and exhausted cases pass; E2E ends with all four metrics zero. |
| PostgreSQL rate limiting | Atomic fixed-window UPSERT per user/operation, independent committed admission transaction, validated parameters and bounded counters. Eight synchronized requests obey the exact admission bound; rollover, rejected attempts, independent scopes and retained replay/conflict semantics pass. |
| Operator commands | Separate role can read diagnostics and call audited lookup/retry. Outbox-first locking preserves worker ordering; active lease returns busy; terminal rows remain terminal; no generation/attempt reset or forced outcome. Scheduling and immutable audit commit together. Actual restricted-login CLI tests pass. |
| Runbook | Documents setup, exit codes, pagination, incident interpretation, safe retries, unknown outcomes, volume preservation, stopping writes, downgrade limits, shared-principal attribution, correlation and throttling. Procedures do not recommend ledger repair or force release. |

The rate limiter is HTTP admission under the trusted API DB role, not a financial
constraint against a compromised runtime principal. Concurrent duplicates before
the first operation commits can consume more than one admission slot; committed
keys preserve replay/conflict behavior. Both boundaries are explicitly documented.

## Global M1–M6 architecture and financial audit

The implementation remains the approved monolith: FastAPI, SQLAlchemy 2, Alembic
and PostgreSQL, with an outbox worker and a persistent HTTP provider simulator.
SQLite is limited to the approved provider simulator. No Redis, Kafka, extra
application database, microservice decomposition or distributed transaction was added.

Python responsibilities are separated into HTTP/schema, service/transaction,
provider/worker and operations modules. Strict mypy, Ruff and formatting checks
pass. Monetary amounts remain bounded integer minor units; currencies do not
convert. The small local retry wrapper is explicit and permits only errors known
to abort a transaction. No unjustified architecture change was identified.

| Invariant / boundary | Concrete protection |
| --- | --- |
| Exactly two wallet accounts | Unique wallet/currency and wallet/account-kind constraints, same-transaction creation and deferred wallet-pair checks. |
| Money conservation and journal balance | M3 pair constraint triggers require exactly two opposite entries. Composite currency foreign keys, integer bounds and read-only NUMERIC reconciliation protect/check currency consistency and totals. |
| Nonnegative user funds | Conditional balance projection, nonnegative CHECK and deterministic UUID-ordered account locks. Competing transfers, payments and mixed debits pass. |
| Immutable financial history | Runtime roles lack direct journal/entry/balance writes and private posting execution; immutable/no-truncate triggers and retained-operation/idempotency rules prohibit mutation. |
| Atomic transfers | Idempotency claim, business row, posting/projections, audit and stored response share one transaction. Commit precedes success response. |
| Atomic payment creation | The same boundary covers payment, available→reserved posting, unique outbox row, audit and stored 202. Creation fault injection and rollback tests pass. |
| API idempotency | Database uniqueness for user/kind/key, typed canonical request hashing, conflict-safe insert followed by row locking and retained response. Identical retries return the initial response; contradictory payloads conflict. |
| Provider idempotency | Payment UUID-derived stable key, SQLite unique identities, BEGIN IMMEDIATE, matching parameters and immutable outcome committed before reply. Restart and concurrent duplicate tests pass. |
| One reservation / terminal outcome | Partial unique indexes enforce one reservation and one terminal journal across settlement OR release; deferred completeness checks match state, accounts, amount and currency. |
| Atomic finalization | Reserved debit, clearing/available credit, terminal state, completed outbox and audit commit together. Backend termination and release-specific audit/outbox failures leave the original reservation intact. |
| Concurrent workers | SKIP LOCKED claims, queue→payment→ordered-account locking, monotonic generations and live-lease/terminal checks. Actual overlapping worker/provider execution rejects generation 1 and accepts generation 2 once. |
| Unknown outcomes | Query before possible same-key resubmission; timeout/5xx/invalid evidence remains unknown. After five pending claims, needs_review retains funds and only queries. Late definitive success/refusal settles/releases once. |
| Session security and ownership | Argon2id passwords, random opaque session tokens stored as SHA-256 digests, expiry/revocation, authenticated ownership filters and not-found behavior for foreign resources. Direct monetary functions recheck supplied ownership. |
| Database privilege separation | Migrator separate from API/worker/operator; safe SECURITY DEFINER search paths, qualified objects and revoked PUBLIC execution. Allowed/forbidden runtime operations and actual restricted logins pass. |
| Audit integrity | Successful state changes and audit share their transaction. Operator audit is immutable and retained; crashes roll back scheduling and audit together. Correlation is transaction-local, with payment fallback on crash-exhaustion audit. |
| No HTTP in financial transactions | Creation and claim commit before HTTP; terminal posting runs in a new local transaction. Connection-pool instrumentation verifies no borrowed DB connection across provider calls or external reconciliation. |

Transaction retries are limited to SQLSTATE 40001/40P01 and three local attempts.
Ambiguous commits are not blindly retried. Actual E2E response loss after committed
transfer/payment creation re-enters retained idempotency and produces no duplicate
financial operation. Database row locks, deterministic ordering and uniqueness
supply the concurrency guarantees; passing tests alone are not their justification.

## Complete test execution

No repository test was filtered out, disabled, marked xfail or skipped by this review.
The full suite used real PostgreSQL and existing HTTP/SQLite/process-crash tests.

| Test group | PASS | FAIL | ERROR | SKIPPED |
| --- | ---: | ---: | ---: | ---: |
| M1 foundation integration | 36 | 0 | 0 | 0 |
| M2 identity and wallet integration | 39 | 0 | 0 | 0 |
| M3 ledger integration | 51 | 0 | 0 | 0 |
| M4 transfer integration | 54 | 0 | 0 | 0 |
| M5 payment integration | 53 | 0 | 0 | 0 |
| M6 resilience integration | 52 | 0 | 0 | 0 |
| All unit tests | 31 | 0 | 0 | 0 |
| **Repository total** | **316** | **0** | **0** | **0** |
| Additional adversarial probes | 11 | 0 | 0 | 0 |
| Complete Compose E2E | 1 | 0 | 0 | 0 |
| **Total pytest cases** | **328** | **0** | **0** | **0** |

Repository duration: **104.54 seconds** (JUnit: 104.538).
Additional suite duration: **10.84 seconds**.
M6 adds 55 repository cases: 52 integration and 3 observability unit cases.
All earlier 261 cases passed on the M6 head. The M1–M5 integration test files are
unchanged by the M6 diff; their assertions were not weakened to obtain this result.

### Additional independent tests

Review-only source was created outside the repository:
`/tmp/test_m6_final_probes.py` and `/tmp/test_m6_final_e2e.py`.

| Scenario | Cases | Main assertions |
| --- | ---: | --- |
| USD/JPY payment success/refusal | 4 | Correct available/reserved/clearing balances, terminal state, stable replay and balanced ledger. |
| Old/new workers overlap around provider acceptance | 2 | Events hold first execution before/after acceptance; expired lease admits successor; one provider row, one terminal journal, old generation rejected. |
| Release finalization faults | 2 | Inject outbox UPDATE or audit INSERT failure; payment stays pending with full reserve, no terminal audit/journal; recovery releases once. |
| Populated M5→M6 migration | 1 | Existing transfer and all four payment states; exact preservation of accounts, journal entries, business rows, outbox, idempotency and pre-existing audit fields; new correlated audit works after upgrade. |
| Payment reconciliation corruption | 2 | Missing reservation and unexpected terminal journal detected after controlled administrator-only corruption; diagnostics leave stored state unchanged. |
| Full Compose journey with response loss | 1 | Actual HTTP registration/authentication/wallet/funding/transfer/payment, background worker/provider, internal and external reconciliation, stable replay after dropped responses and revoked sessions. |

Controlled corruption/fault probes used isolated test databases that the fixtures
dropped afterward. They did not modify the Compose database's financial history.

### Complete real-HTTP E2E evidence

The rebuilt Compose API, PostgreSQL, worker and provider were used, with no mocked
financial transaction or provider call:

1. Register and authenticate two new fictional users; create two EUR wallets.
2. Open a fixed **100000 minor-unit** fixture for the source through the dev/test
   posting role, using separate provisioning credentials outside API/worker runtime.
3. Transfer **12000** source→destination. A local proxy waits for the upstream
   committed response, then closes the downstream connection without returning it.
   The client observes `RemoteDisconnected`; same-key replay returns the stored result.
4. Create a **1500** outgoing payment from the destination. Drop its committed
   response in the same way, then verify identical replay and one payment identity.
5. The actual background worker reaches the persistent HTTP provider and settles.
   Changed payloads with either retained key return conflict.
6. Final available balances are **88000** and **10500**, both reservations are zero,
   and the payment's clearing movement is 1500. The payment has two journals and
   four entries summing to zero; its outbox is done.
7. Internal reconciliation returns no issues. External reconciliation checks all
   **3 payments then present in the local database**, completes in one page and
   returns no findings. Pending/needs_review/expired-leases/outbox-age metrics are zero.
8. Payment-created audit uses the original request correlation; settlement audit
   uses the payment UUID. Both test sessions are revoked; subsequent user lookup is 401.

E2E payment: `7197dd63-c4a2-4216-a708-2514f9f51285`.
E2E transfer: `1f891f3c-1e15-42eb-9645-f17e7860fec3`.

The E2E intentionally leaves two fictional users/wallets, the fixed opening,
transfer, payment and immutable history in the dedicated local development stack.
Funding capability was explicitly provisioned for this test. This stack must not
be represented as the demonstration deployment's no-funding-role configuration;
fresh-migration/runtime privilege tests independently verify that boundary.

### Live log verification

Collected API/worker/provider logs contained **332 JSON event records** at the
observation point. Assertions checked required JSON fields, API request correlation,
worker payment correlation, provider request correlation and a successful applied
terminal event. Generated test passwords, emails and bearer-token canaries did
not appear in the collected logs. Only aggregate results were printed.
Native server lifecycle messages are not counted as application JSON events.

## Migrations, Docker and GitHub Actions

- All relevant migration/privilege tests passed: clean base→head, repeat upgrades,
  empty downgrade/re-upgrade, previous-revision upgrades, readiness/EXECUTE
  revocation and refusal to destroy retained financial/operator history.
- The added populated M5→M6 case verifies existing financial data, not just an
  empty schema. Existing M2/M3/M4 populated upgrade tests also remain green.
- Docker uses a non-root application user, loopback published services,
  separate runtime credentials, persistent PostgreSQL and provider volumes,
  disabled raw Uvicorn access logs and a frozen application dependency build.
- The actual Compose build/start completed successfully. PostgreSQL, API and
  provider were healthy; migration exited 0; worker was running. The worker has
  no application healthcheck, so its successful E2E execution provides liveness
  evidence at test time rather than claiming Docker continuously monitors it.
- CI workflow inspection confirms complete unit/integration suites, lint,
  format/types, Compose startup and HTTP/payment smoke flows. CI jobs have
  read-only repository permissions. Both live PR checks were SUCCESS:
  [38269073129](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38269073129)
  and [38269152381](https://github.com/WYLLLY/payment-ledger-system/actions/runs/38269152381).
  Their state was rechecked before reporting, separately from local evidence.

## Commands and verification results

Working directory: `/Users/william/Documents/Projet 1`.
Local PostgreSQL/network tests used the host access needed for loopback servers.

| Command | Result |
| --- | --- |
| `.venv/bin/pytest --junitxml=/tmp/m6-final-suite.xml > /tmp/m6-final-suite.log 2>&1` | 316 PASS, 0 FAIL/ERROR/SKIPPED |
| `PYTHONPATH="$PWD" .venv/bin/pytest -c pyproject.toml /tmp/test_m6_final_probes.py /tmp/test_m6_final_e2e.py --collect-only -q` | 12 cases collected |
| `PYTHONPATH="$PWD" .venv/bin/pytest -c pyproject.toml /tmp/test_m6_final_probes.py /tmp/test_m6_final_e2e.py -q --junitxml=/tmp/m6-final-additional.xml > /tmp/m6-final-additional.log 2>&1` | 12 PASS, 0 FAIL/ERROR/SKIPPED |
| `.venv/bin/ruff check .` | PASS |
| `.venv/bin/ruff format --check .` | PASS; 79 files already formatted in the local checkout |
| `.venv/bin/mypy` | PASS; 59 source files |
| `UV_CACHE_DIR=.local/uv-cache .venv/bin/uv lock --check --offline` | PASS; 46 packages resolved |
| `/opt/homebrew/bin/actionlint .github/workflows/ci.yml` | PASS |
| `/opt/homebrew/bin/shellcheck docker/init-db.sh` | PASS |
| `git diff --check` | PASS |
| `/opt/homebrew/bin/colima start --profile payment-ledger` | PASS; existing profile restored |
| `DOCKER_CONTEXT=colima-payment-ledger COMPOSE_PROGRESS=plain /opt/homebrew/bin/docker-compose up --build --wait --wait-timeout 180 > /tmp/m6-final-compose.log 2>&1` | PASS |
| `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose ps --all` | Migration exit 0, DB/API/provider healthy, worker running |
| `DOCKER_CONTEXT=colima-payment-ledger /opt/homebrew/bin/docker-compose logs --no-color --no-log-prefix api worker provider > /tmp/m6-final-services.log` | Logs collected; Python JSON/correlation/canary assertions PASS |
| `.venv/bin/python /tmp/m6_slow_trickle_probe.py` | Reviewer: bounded TCP observation PASS; 0.920s versus 0.2s inactivity timeout |
| `gh pr view --json number,url,title,body,baseRefName,baseRefOid,headRefName,headRefOid,files,statusCheckRollup` and `gh pr checks` | PR #4 exact head/base verified; two successful checks |
| `gh pr view 4 --json state,baseRefOid,headRefOid,mergeStateStatus,reviewDecision,statusCheckRollup` | Final recheck: same head/base, OPEN/CLEAN, successful CI |

### Environment issues and evidence retention

Initial `docker-compose ps --all` could not connect because the dedicated Colima
profile was stopped. It was started before running the suite. Initial sandboxed
`gh pr view` could not connect to GitHub; the authorized host-network retry succeeded.
Neither infrastructure issue was counted as passing verification or as a code defect.
No repository test failed during this M6 review execution.

After the chat interruption/resumption, `/tmp` review artifacts were no longer
available: a final attempt to reopen the JUnit file raised `FileNotFoundError`,
and the temporary probe source was also absent. The results above were captured
and inspected before that interruption, including JUnit totals and group counts,
complete test output, E2E result JSON and log-check results. HEAD remained unchanged.
No missing artifact was treated as a new passing execution. This report preserves
the evidence summary; raw temporary scripts/logs/JUnit files are not promised as
durable attachments. Retaining those probes in CI is part of L2.

## Remaining gaps and explicit limits

- The additional twelve scenarios passed but are not committed regression tests.
- A deterministic total-response-deadline regression is missing because that
  mechanism is not implemented; see L1. The finite TCP probe measured current behavior.
- Concurrent request-correlation isolation and middleware errors after response
  start could receive dedicated retained tests. Existing tests cover sequential
  context isolation, generic failures before response start and secret redaction;
  the E2E checks real cross-service correlation. No defect was demonstrated.
- Corruption probes do not exhaust every administrator-created malformed schema
  or journal topology. Runtime invariants are primarily constraints/privileges;
  reconciliation is detection, not protection against a compromised migrator.
- Reconciliation pages are observations, not a distributed atomic snapshot.
  New UUIDs can require a fresh sweep; the runbook documents this. No diagnostics
  automatically repair history or infer refusal from an unavailable provider.
- The operator login is shared. Session-user audit plus incident reason provides
  principal/ticket attribution, not authenticated individual operator identities.
- No host-power-loss, disk-loss, backup restore, production load, external-provider
  certification or dependency vulnerability scan is claimed. Persistent SQLite
  restart, actual worker kill, PostgreSQL backend termination and recovery are tested.
- The two pre-existing standalone local smoke scripts were not rerun separately:
  the new Compose E2E executed the requested funded transfer and payment journey,
  and remote CI reports its own smoke execution. All repository pytest tests ran.
- The project is a fictional-money portfolio backend. Runtime DB principals and
  simulator are trusted boundaries, as specified in the approved architecture.

## Final verdict

**APPROVE.** M6 meets the reviewed resilience and operational acceptance criteria,
and the final M1–M6 audit found concrete protections for financial integrity,
transactional atomicity, privilege separation, idempotency and concurrency.
All 316 repository tests and 12 additional cases passed, including the complete
Compose journey and critical recovery scenarios. The two Low findings are
nonblocking availability/coverage improvements, with no unresolved significant
implementation finding. No code correction or PR merge was performed.
