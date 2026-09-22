# ADR 0002: Database-authoritative competition transitions

## Status

Accepted.

## Context

The first concurrency experiment proved two check-then-write races:

- a submission committed after challenge close;
- submit and cancel both reported success for one submission.

PostgreSQL remained consistent with its declared schema. The missing guarantees
were business rules split across multiple application statements.

## Decision

Use the smallest PostgreSQL mechanism matching each invariant:

1. Terminal submission transitions use one conditional `UPDATE` whose predicate
   requires the current state to be `created`.
2. New-submission acceptance holds `FOR SHARE` on the challenge row through the
   submission insert and commit.
3. Challenge close holds `FOR UPDATE` on that same challenge row through the
   status update and commit.
4. Idempotency keeps its participant-wide unique index and adds a SHA-256
   fingerprint of canonical challenge ID plus metadata.

The database operation—not a prior Python check—selects the winner.

## Why two mechanisms

Terminal state is one mutable row. A conditional UPDATE atomically checks and
changes that row; a separate lock statement would add no correctness.

Challenge status and submission acceptance span two rows/tables. A conditional
insert without locking does not prevent a concurrent status update from
committing against another snapshot. Compatible shared locks preserve
concurrent submissions while an exclusive close lock defines the ordering.

## Lock ordering

Acceptance and close acquire the challenge row before reading the hackathon.
No flow in this decision acquires those resources in reverse order.

## Consequences

- Exactly one competing terminal transition succeeds; losers receive 409.
- Close waits for acceptance transactions that reached the gate first.
- Acceptance transactions behind close observe `closed` and fail.
- Concurrent submissions remain compatible at the challenge lock.
- A slow transaction between challenge lock and commit can delay close.
- The application must keep the locked transaction short.
- Key reuse with different meaningful input becomes a visible conflict.

## Rejected alternatives

- Application-only validation: already disproven by the baseline.
- Locks for terminal transition: larger mechanism than a conditional UPDATE.
- Version column: useful for general editing, unnecessary for one expected
  source state.
- `SERIALIZABLE`: broader cost and retry surface than the local invariants need.
- Application/distributed locks: duplicate database coordination and do not
  improve a PostgreSQL-owned invariant.

## Reconsider when

- submission acceptance includes long-running work inside the transaction;
- hackathon-wide closure must atomically gate every challenge;
- challenge state moves to another datastore;
- general concurrent editing needs a reusable versioning policy;
- measured lock waits make the close/acceptance policy operationally unsuitable.

