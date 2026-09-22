# ChallengeForge concurrency design

Status: implemented and experimentally verified in correctness iteration 2  
Historical baseline (unchanged): [`concurrency-report.md`](concurrency-report.md)

## 1. Business invariants

### Submission terminal state

A submission can make exactly one transition out of `created`:

```text
created -> submitted
created -> cancelled
created -> failed
```

All three target states are terminal. When terminal transitions race, exactly
one database operation succeeds. Every loser receives HTTP 409 and must not
report its requested terminal state as successful.

### Challenge acceptance

A new submission is accepted only if the challenge is `published` at the
authoritative database operation that admits the submission.

“Authoritative” is the transaction that protects the challenge row and inserts
the submission—not an earlier, unprotected Python SELECT.

The two valid orderings are:

```text
acceptance obtains the challenge gate first
-> submission INSERT and COMMIT
-> close obtains the gate and COMMITs
```

or:

```text
close obtains the challenge gate first
-> close COMMITs
-> acceptance observes closed and returns 409
```

It is forbidden for close to commit first and a new acceptance transaction to
commit afterward.

This iteration defines only challenge close. Hackathon close is not implemented.

### Idempotency

The idempotency-key scope remains **participant-wide**, preserving the existing
unique index `(participant_id, idempotency_key)`.

- Same participant + same non-null key + same fingerprint: replay the original
  result.
- Same participant + same non-null key + different fingerprint: HTTP 409.
- Null key: a new attempt every time.

The fingerprint is SHA-256 over canonical UTF-8 JSON containing:

- `challenge_id`;
- submission `metadata`.

Object keys are sorted and compact separators are used. The digest, not the
request payload, is stored. Artifact attachment is a later endpoint and is not
part of submission-create idempotency.

Including `challenge_id` in the fingerprint while retaining participant-wide
key scope means accidental reuse on another challenge conflicts instead of
silently replaying the wrong resource. Changing the unique index to
participant-plus-challenge would permit key reuse but would silently change the
established meaning of a client operation token.

## 2. Baseline races

The original experiment remains in `docs/concurrency-report.md`. These are the
interleavings being corrected.

### Race A — create submission vs close

```text
Acceptance T1: SELECT challenge -> published
Close T2:      UPDATE challenge -> closed
Close T2:      COMMIT
Acceptance T1: INSERT submission
Acceptance T1: COMMIT -> accepted after close
```

The foreign key proves only that the challenge exists.

### Race B — submit vs cancel

```text
Submit T1: SELECT submission -> created
Cancel T2: SELECT submission -> created
Submit T1: Python transition check passes
Cancel T2: Python transition check passes
Cancel T2: UPDATE cancelled; COMMIT; HTTP 200
Submit T1: UPDATE submitted; COMMIT; HTTP 200
Final state: submitted
```

### Race C — cancel vs submit

This is the reverse write order of Race B:

```text
Cancel T1: SELECT submission -> created
Submit T2: SELECT submission -> created
Submit T2: UPDATE submitted; COMMIT; HTTP 200
Cancel T1: UPDATE cancelled; COMMIT; HTTP 200
Final state: cancelled
```

Both races violate the API history even though the final status string satisfies
the database check constraint.

### Race D — same key, different payload

```text
T1: SELECT participant + key -> absent
T2: SELECT participant + key -> absent
T1: INSERT metadata A; COMMIT
T2: INSERT metadata B -> unique violation
T2: ROLLBACK; SELECT T1 row; HTTP 200 replay
```

The baseline loses the fact that T2 represented a different request.

## 3. Transaction boundaries

### Terminal transition

```text
REQUEST TRANSACTION
-> UPDATE submissions
   SET status = target, updated_at = now
   WHERE id = submission_id
     AND participant_id = actor
     AND status = created
   RETURNING row
-> one row: COMMIT and HTTP 200
-> zero rows: SELECT current row to classify 404 / 403 / 409
```

The conditional UPDATE is the authoritative transition.

### New submission acceptance

Idempotent replay is checked before admission because replay does not create a
new submission.

```text
REQUEST TRANSACTION
-> SELECT existing participant + idempotency key
-> matching replay: return original
-> fingerprint mismatch: 409
-> SELECT challenge FOR SHARE
-> validate challenge == published
-> SELECT hackathon and validate published
-> INSERT submission
-> COMMIT (releases challenge share lock)
```

### Challenge close

```text
REQUEST TRANSACTION
-> SELECT challenge FOR UPDATE
-> SELECT containing hackathon
-> validate organizer and current challenge state
-> UPDATE challenge = closed
-> COMMIT (releases exclusive lock)
```

Both flows acquire the challenge row before reading the hackathon, maintaining a
consistent lock order.

## 4. State machine mechanism

Chosen: one conditional database UPDATE.

It protects “only `created` can transition” because predicate evaluation and
the write are one PostgreSQL operation. Under `READ COMMITTED`, a competing
UPDATE waits for the row; after the winner commits, PostgreSQL re-evaluates the
loser's predicate against the new row version and affects zero rows.

The loser then reads current state only to produce the correct error. That
follow-up SELECT is not part of correctness.

## 5. Idempotency mechanism

Chosen: existing partial unique index plus stored request fingerprint.

The Python fast-path compares fingerprints for sequential retries. The unique
index remains authoritative when two requests see no existing row. A loser
rolls back, reads the winner, compares fingerprints, and either replays or
returns 409.

The migration backfills fingerprints for existing keyed submissions from their
stored challenge ID and metadata, then adds a check requiring key and
fingerprint to be null or non-null together.

## 6. Challenge-close mechanism

Chosen: compatible shared row locks for acceptance and an exclusive row lock for
close.

Multiple submissions may hold `FOR SHARE` concurrently. Close's `FOR UPDATE`
waits for all admitted acceptance transactions. Conversely, acceptance waits
behind an authoritative close and then reads the new `closed` row version.

This lock protects only the short acceptance transaction. It does not cover
artifact upload, evaluation, or participant work.

## 7. Why the database is authoritative

PostgreSQL continues to enforce:

- foreign keys;
- status vocabulary checks;
- primary and idempotency uniqueness;
- atomic commit/rollback;
- conditional-update winner selection;
- challenge-gate lock ordering.

Python orchestrates error classification and response mapping but does not use
process-local locks for shared correctness.

## 8. Rejected alternatives

### Constraint only

A normal check constraint cannot compare a submission insert with mutable state
in another table. PostgreSQL check constraints are row-local and are not a
cross-transaction admission gate.

### `INSERT ... SELECT WHERE challenge.status = published` without a lock

A single statement can still use a snapshot in which the challenge is
published while a concurrent close updates and commits. It does not by itself
order the two authoritative operations.

### Lock every submission transition

`SELECT FOR UPDATE` followed by validation would work, but the conditional
UPDATE is one statement, holds the lock for less time, and directly reports the
winner through affected-row count.

### Optimistic version column

A version would generalize to arbitrary edits, but terminal transition needs
only one expected state. Adding a version is unnecessary mechanism in this
slice.

### `SERIALIZABLE`

Global stronger isolation would introduce retry obligations for unrelated
transactions. The invariants can be protected locally with one conditional
UPDATE and one well-defined row gate.

### Advisory/distributed/application locks

They duplicate PostgreSQL ownership, add lifecycle failure modes, or fail across
processes. The protected state already lives in PostgreSQL.

## 9. Failure behavior

- Lost terminal transition: HTTP 409 `invalid_state_transition`, including the
  current status when available.
- New submission after close wins: HTTP 409 `conflict`.
- Same idempotency key with a different challenge or metadata: HTTP 409
  `conflict`.
- Same key and fingerprint: original submission with HTTP 200 replay.
- Missing row remains 404; wrong participant remains 403.

## 10. Performance impact

Measured results are recorded after running
`scripts/correctness_experiment.py` and written to
`docs/concurrency-correctness-results.json`.

The expected costs are:

- conditional terminal UPDATE: one fewer correctness SELECT on the success
  path, with losers waiting only on the target submission row;
- acceptance lock: one row lock held from challenge validation through insert
  commit; concurrent acceptance share locks remain compatible;
- close: waits for already-admitted acceptance transactions;
- fingerprint: one canonical JSON serialization and SHA-256 digest per keyed
  create, plus one text column.

See “Measured cost” below for the actual run.

## 11. Remaining limitations

- Hackathon close has no authoritative gate.
- Metadata/artifact update and terminal transition policy after challenge close
  remains a product question; this iteration gates **new acceptance**.
- Artifact bytes remain outside the database transaction and can be orphaned.
- Idempotency does not fingerprint later artifact attachment.
- There is no leaderboard or scoring transaction to protect.
- Ordering still uses `created_at DESC` without an ID tie-breaker.
- Real authentication is absent.
- The close guarantee assumes challenge status changes use the application
  transaction. Privileged direct SQL that updates status without taking the
  challenge gate can bypass it.
- This design provides named guarantees; it does not make the system generally
  “race-condition free.”

## 12. Measured cost

Raw results: [`concurrency-correctness-results.json`](concurrency-correctness-results.json)

Environment: PostgreSQL 16.2, `READ COMMITTED`, pool 5 + 5, isolated embedded
PostgreSQL on the same laptop. The HTTP client and application shared a Python
process. These are experiment measurements, not production capacity claims.

### Correctness outcomes

- Terminal races, submit-first: 20/20 winners returned 200, 20/20 losers
  returned 409, and 20/20 final states matched submit.
- Terminal races, cancel-first: 20/20 winners returned 200, 20/20 losers
  returned 409, and 20/20 final states matched cancel.
- Challenge gate, submission-first: 10/10 submissions were accepted and every
  close completed afterward.
- Challenge gate, close-first: 10/10 closes succeeded and 10/10 submissions
  returned 409.
- Conflicting fingerprints: 20/20 pairs produced one 201 plus one 409 and
  exactly 20 database rows.
- Identical keyed requests: 50 concurrent requests produced one row and one ID
  (one 201, forty-nine 200 replays).
- Mixed workload: each of 20 terminal pairs produced exactly one 200 and one
  409; 21 creates won the challenge gate and 19 lost; ten requests begun after
  close all returned 409. Foreign-key violations, illegal statuses, and
  duplicate key groups were all zero.

### Latency and throughput

Historical baseline:

- duplicate fanout (20 requests): p50 400.77 ms, p95 994.48 ms; p99 and exact
  throughput were not captured;
- independent fanout (100 writes): p50 2228.72 ms, p95 3728.77 ms.

Correctness run:

- duplicate fanout (50 requests): p50 1780.59 ms, p95 3070.23 ms, p99
  3352.10 ms, 14.03 requests/s;
- independent fanout (100 writes): p50 3543.59 ms, p95 6293.64 ms, p99
  6421.45 ms, 14.43 requests/s;
- deliberately ordered terminal races: p50 116.57 ms, p95 187.45 ms, p99
  380.99 ms;
- deliberately ordered close/acceptance races: p50 155.57 ms, p95 211.29 ms,
  p99 221.31 ms;
- conflicting fingerprints: p50 79.58 ms, p95 312.20 ms, p99 334.98 ms.

The comparable 100-write p95 increased by 68.8% in this run. The shared lock is
the primary intentional change on that path, but a before/after pair on one
laptop does not isolate causality from run-to-run scheduling noise. The correct
conclusion is “a material end-to-end increase was observed and must be
profiled,” not “the row lock costs exactly 68.8%.”

### Database and process observations

For the 100-write correctness run:

- exact peak pool checkouts: 4 (baseline 2);
- maximum sampled transaction: 97.27 ms (baseline 89.61 ms);
- sampled lock waiters: 0 because acceptance share locks are compatible;
- deadlocks and database conflicts: 0;
- maximum sampled application/harness CPU: 135.9%;
- maximum sampled database CPU: 33.2%;
- application/harness RSS: 92.17 MiB;
- summed PostgreSQL RSS: 264.19 MiB.

The deliberately blocked race tests observed two lock waiters, proving that
losers wait at the database authority point. Across all scenarios there were no
deadlocks or pool timeouts.

### Guarantees demonstrated—not universal claims

Tested:

- both terminal winner orderings;
- both challenge-gate orderings;
- identical and conflicting idempotent requests;
- mixed create/transition/read/close activity.

Not tested:

- process or database crash during these operations;
- multiple application processes;
- network partitions;
- artifact upload races;
- hackathon-wide close;
- future scoring/leaderboard writes.

The system is not claimed to be race-condition free. It now provides the named
submission-transition, challenge-acceptance, and keyed-request guarantees under
PostgreSQL transactions.

## 13. Implementation map

- `application/submissions.py`: canonical fingerprint comparison, idempotency
  conflict behavior, locked acceptance, and conditional terminal transitions.
- `application/challenges.py`: authoritative organizer close use case.
- `persistence/repositories.py`: `FOR SHARE` acceptance gate, `FOR UPDATE`
  close gate, and `UPDATE ... WHERE status = created RETURNING`.
- `persistence/models.py` and Alembic revision
  `0002_concurrency_correctness`: fingerprint column and key/fingerprint pairing
  constraint.
- `api/routes/challenges.py`: `POST /api/v1/challenges/{id}/close`.
- `tests/test_concurrency_correctness.py`: deterministic database ordering tests
  for both terminal and close/acceptance winner orders.
- `scripts/correctness_experiment.py`: repeated HTTP-level adversarial races,
  mixed invariant checks, metrics, and baseline comparison.

## 14. System-design lesson

“Use the database” is not one mechanism. The shape of the invariant determines
the smallest useful primitive:

- a same-row expected-state transition maps naturally to a conditional UPDATE;
- a cross-row admission rule needs an ordering point on the shared parent row;
- request identity needs both uniqueness and semantic equivalence.

Constraints keep rows structurally valid, but they do not automatically encode
history or temporal meaning. Conversely, global stronger isolation is not
required when the application can name the exact authority point.

## 15. Next problem earned

The next investigation is the observed write-latency increase, especially the
100-write p95 change from 3728.77 ms to 6293.64 ms while shared challenge locks
showed no sampled lock wait. Profile separately:

- SQL statement timing and lock-manager time;
- event-loop and JSON/logging time;
- load generator in a separate process;
- repeated before/after runs on the target laptop;
- why the application pool peaked at only four checkouts under 100-request
  fanout.

That measurement should determine whether the cost is the new challenge-row
gate, embedded-PostgreSQL scheduling, or another existing bottleneck. It does
not yet justify a cache, queue, or service split.

