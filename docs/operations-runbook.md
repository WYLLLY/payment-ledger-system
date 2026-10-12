# M6 operations and recovery runbook

These tools operate on fictional funds. Use the same PostgreSQL database and persistent
provider volume as the API and worker. Never delete either volume to resolve an incident.
The approved plan remains the architectural source of truth.

## Access and setup

Migrate once with the separate migrator, before starting M6 runtime. The migration creates
`payment_operator` (NOLOGIN, no administrative privileges). The API and worker cannot use
its functions. Operator credentials are not passed to either Compose service.

An administrator may provision the dedicated operator login from a trusted host:

```sh
# Read a separate generated secret interactively; do not put it in shell history.
read -r -s OPERATOR_DB_PASSWORD
export OPERATOR_DB_PASSWORD
uv run --frozen python -m scripts.provision_operator
```

The provisioning script alone uses `MIGRATION_DB_PASSWORD` from the existing protected
`.env`. Runtime commands use `OPERATOR_DB_PASSWORD` and the fixed login
`payment_operator_login`, never the migrator. Supply `OPERATOR_DB_HOST`, `OPERATOR_DB_PORT`
and `OPERATOR_DB_NAME` when differing from loopback:15432/payment_ledger. Protect shell
access and secrets. The shared login identifies the operator principal in `session_user`;
the required reason should reference an incident ticket identifying the human. Reasons are
1–200 ASCII letters, digits, spaces or `._:#/-`; do not include personal data or secrets.

The operator can read financial diagnostics and append audit events only through the
approved function. It cannot read passwords/sessions, post journals, finalize payments,
modify balances, reset attempts, steal a live lease, or become the worker/migrator.

## Observe before acting

```sh
uv run --frozen python -m payment_ledger.operations.cli metrics
uv run --frozen python -m payment_ledger.operations.cli reconcile --limit 100
uv run --frozen python -m payment_ledger.operations.cli lookup PAYMENT_UUID --reason INC-123
```

Commands emit JSON. `metrics` exposes internal counts `pending`, `needs_review`,
`expired_leases`, and `oldest_outbox_age_seconds` (since payment creation, excluding completed
outbox rows; zero for an empty queue). There is no public metrics HTTP endpoint.

`reconcile` checks projected balances against entries, balanced two-entry journals,
reservation/terminal account pairs, payment/outbox agreement and reserved balances against
unresolved payments in **one PostgreSQL snapshot**. It then checks remote identities and
terminal outcomes outside database transactions. It does not mutate payments or the ledger.

The external pass checks at most `--limit` payments (1–1000). If `complete` is false, repeat
with `--after NEXT_CURSOR`; preserve every page's findings. A page marked complete means
it reached the end of that page's observed data, not a globally atomic external snapshot.
Start a new sweep without a cursor to include newly created payments. Schedule this CLI
through your existing operator process if periodic reports are needed; no new scheduler
or infrastructure is installed.

Exit codes: 0 means the command completed (for reconciliation, no finding on the checked
page and no next page); 1 means reconciliation needs attention; 2 means the database command
failed. Invalid arguments are rejected by argparse. A successful `lookup` command may
report `unknown` or `not_found`; neither means a failed payment.

| Finding | Interpretation and action |
| --- | --- |
| `balance_mismatch`, `invalid_journal`, `currency_not_conserved`, `negative_user_balance` | Internal accounting discrepancy. Stop financial writes and investigate with an administrator; retain evidence. No automatic repair. |
| `payment_journal_mismatch`, `unexpected_terminal_journal`, `payment_outbox_mismatch`, `reserved_balance_mismatch` | Payment/ledger/outbox inconsistency. Stop writes, preserve PostgreSQL and provider state, investigate migrations or privileged access. |
| `provider_unavailable` | Unknown remote outcome. Restore connectivity, then check again. Keep funds reserved. |
| `unresolved_reservation` | Remote absence for a pending/needs_review payment. Never infer definitive refusal. Check worker health and retry budget. |
| `awaiting_finalization` | Remote terminal evidence exists but local finalization is pending. Request recovery below. |
| `concurrent_progress` | Worker changed the local row during HTTP lookup. Recheck; this is not proof of corruption. |
| `provider_mismatch` | Local terminal state contradicts the provider or the provider has lost the identity. Stop and investigate. Do not rewrite either ledger or provider history. |

## Safe recovery

```sh
uv run --frozen python -m payment_ledger.operations.cli retry PAYMENT_UUID --reason INC-123
```

This transaction locks the outbox row and appends an immutable operator audit event.
`scheduled` makes it due now; `busy` leaves a live lease untouched; `terminal` does nothing
to an already completed payment. The command never resets the generation, attempts, or
payment state. Repeating it after losing the command response is safe; each invocation has
its own audit record. A crash before commit rolls back both scheduling and audit.

The worker then claims a new generation, commits, and queries the provider. Pending payments
may submit with their original provider identity within the five-attempt budget.
`needs_review` payments **only query**. Absent or unavailable provider state leaves the
reserve intact; only validated terminal evidence permits settlement or release. If the
provider permanently has no outcome after exhaustion, M6 deliberately provides no force
release or reset command. This requires a separately approved business decision.

| Interruption | Recovery |
| --- | --- |
| API response lost after commit | Repeat the same request with the same Idempotency-Key and canonical payload. |
| Worker crashes after claim, before HTTP | Restart worker, allow lease expiry. Next claim queries provider first. |
| Provider accepts but response is lost | Keep reservation. Recover using provider lookup with the same payment identity. |
| Worker crashes before/after local commit | Restart and reclaim through outbox; never directly replay a posting or HTTP call. Unique terminal journal and generation checks fence duplicates. |
| Operator interrupted | Re-run lookup/retry with the incident reason. Inspect operator audit if command outcome is uncertain. |
| PostgreSQL unavailable | Restore database availability. Readiness fails closed; do not switch to migration credentials or bypass constraints. |

Stop/start the worker without removing data:

```sh
docker compose stop worker
docker compose up -d worker
```

For a suspected accounting discrepancy, stop API and worker first, preserve snapshots of
both databases, collect correlation/payment IDs and reports, and escalate. Any history
repair requires a reviewed, explicit procedure outside this milestone. M6 downgrade refuses
to delete nonempty operator audit. Empty-operator-audit downgrade removes M6 counters and
correlation fields, so perform it only on disposable databases or after an approved rollback
assessment. Cluster-wide roles survive downgrade.

## Correlation, logs and throttling

Application, worker and simulator events are JSON with UTC timestamp, level, event and
correlation ID. The API accepts only UUID `X-Request-ID` values (otherwise generates one)
and returns it. API financial audit stores the request ID; its resource ID links to the
payment. Worker/provider flows use the immutable payment UUID. Request logs use route
templates, not raw paths, headers, bodies, query strings, recipients, credentials or SQL
exception text. Uvicorn access logging is disabled in Compose to avoid duplicating raw URLs.
Uvicorn lifecycle and PostgreSQL's own server logs retain their native formats.

Financial POST admission uses PostgreSQL counters per authenticated user and operation kind:
60 requests/60 seconds by default, bounded through `APP_FINANCIAL_RATE_LIMIT` and
`APP_FINANCIAL_RATE_WINDOW_SECONDS`. Exceeding the fixed-window quota returns 429 with
`Retry-After` before a new financial operation is created. Counts commit independently, so
business rejection or interruption can consume a slot. Already committed idempotency keys
bypass admission and retain their original replay/conflict behavior. Concurrent attempts
before the first commit may consume multiple slots or be throttled; retry the same key later.
The trusted API role enforces this HTTP admission boundary; the counter is not a ledger
invariant or a protection against a compromised database runtime principal. Fixed windows
permit bursts across a boundary. No Redis is involved.

## Verification and limits

Run `uv run --frozen pytest`, `ruff check .`, `ruff format --check .`, and `mypy` using the
locked environment and real dedicated PostgreSQL. CI includes all M1–M6 integration,
concurrency, privilege, migration, HTTP-provider and process-crash tests.

HTTP provider timeouts remain socket inactivity timeouts, not a hard total-response deadline.
A slow trickle can hold a worker longer than its lease; fencing and provider idempotency protect
financial correctness, but throughput may degrade. A report's findings need operator review;
reconciliation is not a substitute for PostgreSQL constraints or an atomic distributed snapshot.
