# Independent review archive

These reports preserve what independent reviewers observed at a specific revision.
Statements about open pull requests, local branches, missing remotes or uncommitted
files describe the review date, not the current repository. Line references also
refer to the reviewed code. Original findings and execution totals are unchanged.

The current maintained suite has **317 tests**. M1–M6 are merged into `main`;
see the [final verification record](../docs/final-verification.md) for current evidence.
Temporary probes are separate from maintained tests and are not all retained in CI.
Archiving a report does not rerun its tests or restore its temporary artifacts.

| Report | Reviewed scope / revision | Historical evidence |
| --- | --- | --- |
| [M2](M2-review.md) | Identity/wallet implementation and corrections | Original review and correction evidence. |
| [M3](M3-review.md) | Ledger, fixtures and invariants | Original ledger review evidence. |
| [M4](M4-review.md) | Transfer PR #2 at `77a6330` | 196 maintained tests + 8 additional probes. |
| [M5](M5-review.md) | Payment PR #3 at `0aaa08` | 261 maintained tests + 14 additional probes. |
| [M6 implementation review](M6-review.md) | Resilience, operations and financial boundaries | Implementation-stage independent review. |
| [Final M1–M6 audit](M6-final-review.md) | M6 PR #4 at `deb99fb` | 316 maintained tests + 12 additional cases. |

The final audit predates the clean-install fixture correction `2558dce`, whose
additional PostgreSQL regression raised the maintained count from 316 to 317.
That correction and the final documentation received separate independent review;
their verification is described in the final record. The audit's two Low findings
(socket-inactivity timeout and unretained composed probes) remain documented limits.

The M4/M5 expanded reports and final M6 audit were initially kept as local review
work. They are archived here during portfolio finalization with their contents intact.
