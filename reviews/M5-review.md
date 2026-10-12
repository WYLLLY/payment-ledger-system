# M5 — Independent review

Date: 11 October 2026 (Europe/Paris). **Verdict: APPROVE.**

Reviewer: separate agent `m5_independent_review`, applying `adversarial-review`
and `financial-invariant-review`. Scope: M5 compared with M4 commit `77a6330`,
including migration, reservation service, API, provider, worker, provisioning,
Compose and tests. The reviewer changed no files or Git state. The pre-existing
uncommitted `reviews/M4-review.md` change was excluded and preserved.

## Findings and corrections

1. **Medium — HTTP protocol failures could stop the worker.** `BadStatusLine`
   and `IncompleteRead` were not caught at the provider boundary, including
   exceptions while reading an HTTP 404 body. Both exception paths now catch
   `http.client.HTTPException` and return `unknown`. Funds remain reserved and
   durable retries/reconciliation continue. Twelve unit cases cover malformed,
   oversized, mismatched and truncated evidence; the reviewer independently
   reproduced six real TCP cases after correction with no escaping exception.
2. **Test determinism — crash after provider acceptance.** The initial process
   crash test depended on a 200 ms response delay. An Event now blocks the
   provider response after the SQLite commit until the test kills and joins the
   worker process. This provides deterministic proof of the intended crash window.

No significant finding remains open after correction. The reviewer also read
the additional populated-M4 migration, real worker login, transfer/payment
contention and no-DB-connection-during-HTTP tests.

## Financial and reliability properties examined

| Property | Concrete protection |
| --- | --- |
| Atomic creation | One PostgreSQL transaction combines the idempotency claim, payment, reservation pair, projected balances, unique outbox task, audit and stored 202 response. |
| Concurrent idempotency | Unique `(user_id, operation_kind, key)`, `INSERT ON CONFLICT`, row lock and server-computed canonical hash; the original response remains immutable. |
| Money conservation | Private `post_pair`, exactly-two balanced-entry deferred checks, matching-currency FKs, conditional debit and nonnegative user-balance CHECKs. |
| One terminal effect | Unique partial terminal-journal index, payment state/identity protection and deferred reservation/outcome completeness checks. |
| Lock ordering | Creation locks idempotency before account UUIDs; claim/finalization consistently lock outbox, then payment, then account UUIDs. |
| Concurrent workers | `SKIP LOCKED`, monotonically increasing generation, live-lease validation, terminal guard and atomic outbox completion. |
| Provider idempotency | Immutable payment-derived key; SQLite `BEGIN IMMEDIATE`, unique identity/key, immutable terminal rows, commit before HTTP reply. |
| Unknown outcome | Status lookup before every possible submission, same provider key, bounded claims, retained reservation and lookup-only `needs_review` reconciliation. |
| Privilege boundary | API can create payments; worker can claim/finalize; neither has direct ledger/outbox/payment writes. Provider evidence is checked against immutable request fields. |
| Recovery and migrations | Crashes before HTTP, after provider acceptance and before local commit; expired leases; populated M4 upgrade and refusal to downgrade retained payment history. |

## Independent execution

```sh
.venv/bin/pytest tests/integration/test_payments.py tests/unit/test_payment_provider.py -q
```

**65 passed in 25.22s**, no skips, using real PostgreSQL, HTTP and SQLite.
The reviewer additionally sent six real TCP responses: invalid status line,
truncated chunked HTTP 200 and truncated chunked HTTP 404, each through lookup
and submission. All six returned `unknown` without exception after correction.

The first independent relaunch after an interruption reported 13 passed and
52 setup errors because PostgreSQL was stopped. After the dedicated Colima
profile and database restarted, all 65 tests passed. The earlier 48-case M5
suite had also passed independently before the additional tests were added.

## Limits

The reviewer did not personally rerun the entire M1–M5 suite or the Compose
payment smoke; the implementation verification report records those separate
executions. Socket timeouts are not absolute total deadlines against a peer
that continuously trickles data. Fencing and provider idempotency still protect
late results. The application/worker DB roles and the simulated provider remain
trusted boundaries; no protection against a compromised migrator is claimed.

This is an independent agent review, not a GitHub approval by the PR author.
It authorizes no merge. M6 was not reviewed or implemented.
