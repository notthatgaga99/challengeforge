# ChallengeForge concurrency report

Run date: 2026-09-21  
Raw results: [`concurrency-results.json`](concurrency-results.json)  
Harness: [`../scripts/concurrency_experiment.py`](../scripts/concurrency_experiment.py)

This is a failure analysis of the existing modular monolith. No correctness
mechanism or infrastructure was added to the application.

## Executive result

PostgreSQL preserved its declared constraints: every persisted submission had
valid foreign keys, duplicate idempotency keys produced one row, and no
deadlocks or database conflicts were observed.

Two business invariants were not protected:

1. A submission already in flight committed **757.10 ms after** its challenge
   had been changed to `closed`.
2. Concurrent `submit` and `cancel` calls both returned HTTP 200. One response
   reported `submitted`, one reported `cancelled`, and the final database state
   was `submitted`.

These are not PostgreSQL consistency failures. They are check-then-write races:
the database schema knows the allowed status vocabulary, but not the
cross-row/time-dependent rules the Python code assumes.

## Test design and limits

The repeatable harness:

- starts the unmodified FastAPI application through its normal lifespan;
- uses PostgreSQL 16.2 at the observed default `READ COMMITTED` isolation;
- keeps the configured SQLAlchemy pool (`pool_size=5`, `max_overflow=5`);
- seeds an organizer, the existing participant, and 100 experiment participants;
- drives real HTTP requests with up to 220 client connections;
- samples `pg_stat_activity`, `pg_stat_database`, SQLAlchemy pool checkouts,
  process CPU/RSS, transaction age, lock waits, conflicts, and deadlocks;
- writes raw JSON to `docs/concurrency-results.json`.

The default database is an isolated embedded PostgreSQL because Docker Desktop
on this machine requires an organization login. It is PostgreSQL, not SQLite or
an in-memory substitute. `EXPERIMENT_DATABASE_URL` can point the destructive
harness at another isolated PostgreSQL database.

Important limits:

- This run used 100 simultaneous independent writes and 60 writes plus readers,
  not a full 500-user event.
- The application and HTTP client harness shared one Python process, so the
  reported application CPU includes load generation.
- PostgreSQL memory is the sum of visible postgres processes.
- Fast scenarios completed before the polling monitor produced a sample; exact
  SQLAlchemy pool event counters are still present for those scenarios.
- `pg_stat_statements` was not installed, so per-SQL query latency was not
  collected.
- The challenge-close test used direct SQL because the current API has no close
  endpoint. The status value and repository write path exist; the direct update
  models that missing organizer operation without inventing its semantics.
- No leaderboard or score model exists, so leaderboard consistency was not
  testable.

Run it with:

```powershell
.\.venv\Scripts\python scripts\concurrency_experiment.py
```

## Part 1: current invariants

### 1. A submission references a valid participant

**Intended invariant.** `participant_id` identifies an existing user whose role
is `participant`.

**Enforcement.**

- Application: `require_participant(actor)` runs before submission creation.
  `participant_id` is copied from `CurrentUser`; it is not client-controlled.
- Database: `submissions.participant_id` is non-null and has a foreign key to
  `users.id`.
- Database limitation: the foreign key proves “existing user,” not
  “participant role.” The role check constraint only validates each user's role
  vocabulary and cannot express the cross-table rule.

**Concurrency behavior.** API creation preserves this with the current immutable
identity flow. If roles become mutable, changing a user to organizer after the
Python role check would not invalidate or prevent the insert. No such role
mutation endpoint currently exists.

### 2. A submission references a valid challenge

**Intended invariant.** `challenge_id` must exist.

**Enforcement.**

- Application: submission create selects the challenge and returns 404 if
  absent or not currently published.
- Database: a non-null foreign key references `challenges.id`, with cascade
  delete.

**Concurrency behavior.** The foreign key is authoritative for existence. A
concurrent delete and insert are coordinated by PostgreSQL's foreign-key
locking. Challenge *status* is a separate invariant and is not covered by the
foreign key.

### 3. Idempotency key semantics

**Intended invariant.** For one participant and one non-null idempotency key,
there is at most one submission row.

**Enforcement.**

- Application fast path: select by `(participant_id, idempotency_key)` and
  replay an existing row.
- Database authority: a partial unique index on those two columns when the key
  is non-null.
- Application race recovery: catch `IntegrityError`, rollback, select the
  winner, and return it as a replay.

**Concurrency behavior.** Preserved in Scenario A: 20 concurrent requests
returned one 201 and nineteen 200 responses, all with one ID; the database had
one row.

**Semantic limits.**

- Scope is participant-wide, not participant-plus-challenge. Reusing a key on a
  different challenge replays the earlier submission.
- Request payload is not fingerprinted. The same key with different metadata
  returns the first result rather than reporting a conflict.
- A null key deliberately has no uniqueness protection.
- With concurrent artifact-bearing requests, each contender writes a different
  file before attempting the database insert. The unique-index loser replays
  the winner's row after rollback, leaving its own file orphaned.

### 4. Submission state transitions

**Intended invariant.** Only `created -> submitted|cancelled|failed` is legal;
terminal states cannot transition.

**Enforcement.**

- Application only: Python reads the row, calls `transition_submission`, then
  assigns the target status.
- Database: a check constraint validates only the four allowed status strings.
  It does not validate transition history.

**Concurrency behavior.** Violated in Scenario F. Submit and cancel both read
`created`, both passed the Python check, and both wrote. Both returned 200; the
last database update won.

### 5. Challenge open/closed state

**Intended invariant.** New submissions require both hackathon and challenge to
be `published`.

**Enforcement.**

- Application only: two SELECTs check current status before idempotency lookup
  and insert.
- Database: check constraints validate status vocabulary, but the submission
  foreign key does not require `published`.

**Concurrency behavior.** A request starting after close returned 404. A
request that passed validation before close committed after close. The meaning
of “closed” at the boundary is therefore not defined or enforced atomically.
Additionally, only submission **creation** checks hackathon/challenge status.
Metadata update, artifact attachment, submit, and cancel load only the
submission row, so an existing `created` submission can still be changed or
submitted after its challenge is closed.

### 6. Participant ownership

**Intended invariant.** A participant can read or mutate only their own
submission.

**Enforcement.**

- Create takes the participant ID from server-side identity.
- Read/update/transition compare `row.participant_id` with `actor.id`.
- Organizer reads additionally verify ownership of the containing hackathon.
- Database stores the relationship but has no row-level authorization policy.

**Concurrency behavior.** Stable while ownership is immutable. There is no
ownership update operation. Database access outside the application can bypass
authorization, as expected for this architecture.

### 7. Submission timestamps

**Intended invariant.** Creation sets `created_at == updated_at`; mutation moves
`updated_at` forward while retaining `created_at`.

**Enforcement.**

- Application generates timezone-aware UTC values.
- Database columns are non-null `timestamp with time zone`.
- The database has no default, monotonicity check, or trigger.

**Concurrency behavior.** Scenario B produced 100 rows with 100 distinct
creation timestamps. This does not prove a strict ordering guarantee. Host clock
changes, direct writes, or equal-resolution timestamps can violate monotonic or
total ordering assumptions.

### 8. Leaderboard/score consistency

No leaderboard, score table, score endpoint, or ranking query exists. There is
no current invariant to test.

### 9. Uniqueness constraints

Current uniqueness:

- primary keys on users, hackathons, challenges, criteria, and submissions;
- challenge specification primary key on `challenge_id` (one specification per
  challenge);
- partial unique idempotency index per participant and non-null key.

Not unique:

- hackathon/challenge titles;
- criterion names;
- participant/challenge pair (multiple attempts are intentional);
- timestamps.

### 10. Ordering assumptions

Submission lists use `ORDER BY created_at DESC`. There is no secondary
tie-breaker and no pagination snapshot. Equal timestamps therefore have
undefined relative order. Scenario B observed no equal timestamps, but the
query does not guarantee a strict total order.

## Part 2: actual transaction boundaries

### Session and connection lifecycle

- `get_db_session` creates one `AsyncSession` in a yield dependency.
- FastAPI caches that dependency within a request, so identity and use-case
  dependencies share the same session.
- SQLAlchemy autobegins the transaction on the first database statement.
- Mutation services call `commit()` explicitly.
- Submission create catches `IntegrityError` and calls `rollback()` explicitly.
- Other exceptions have no explicit handler-level rollback; leaving the session
  context closes it and rolls back an active transaction.
- Read-only requests do not commit; session close rolls back their read
  transaction.
- `expire_on_commit=False` keeps the session's in-memory row values after
  commit.
- No isolation level is configured. PostgreSQL reported `read committed`.
- Pool: size 5, overflow 5, pre-ping enabled; SQLAlchemy's default pool timeout
  applies.

### Create submission timeline

```text
REQUEST
-> SELECT user (development identity; transaction autobegins)
-> SELECT challenge
-> SELECT challenge specification
-> SELECT evaluation criteria
-> SELECT hackathon
-> validate both statuses == published
-> SELECT submission by participant + idempotency key (when supplied)
-> INSERT submission (flush)
-> COMMIT
-> return 201
```

Concurrent duplicate recovery:

```text
INSERT loses unique-index race
-> PostgreSQL raises unique violation
-> ROLLBACK
-> SELECT winner by participant + key
-> return winner as 200 replay
-> request cleanup rolls back the final read-only transaction
```

### Update submission metadata/artifact timeline

```text
REQUEST
-> SELECT user
-> SELECT submission
-> validate owner and status == created
-> optional filesystem write
-> set metadata/artifact key and updated_at
-> UPDATE during COMMIT
-> COMMIT
-> return session-cached row
```

The filesystem write is outside PostgreSQL atomicity.

### Submit/cancel timeline

```text
REQUEST
-> SELECT user
-> SELECT submission
-> validate owner
-> validate transition in Python
-> set status + updated_at
-> unconditional UPDATE during COMMIT
-> COMMIT
-> return session-cached row
```

The UPDATE predicate identifies only the primary key; it does not assert that
the database status is still `created`.

### Publish challenge timeline

```text
REQUEST
-> SELECT user
-> SELECT challenge + specification + criteria
-> SELECT hackathon
-> validate organizer, draft state, and publishability
-> SELECT challenge + related rows again in save_status
-> UPDATE challenge status + updated_at
-> COMMIT
```

There is no close-challenge or close-hackathon API/use case.

## Part 3–5: scenarios and observations

### Scenario A — concurrent duplicate request

**Design.** Twenty simultaneous POSTs, same participant, challenge, key, and
payload.

**Expected.** Exactly one logical submission.

**Observed.**

- 1 response `201`, 19 responses `200`;
- 1 distinct response ID;
- 1 database row;
- p50 400.77 ms, p95 994.48 ms, max 1097.51 ms;
- exact peak app pool checkouts: 2; no client errors.

**PostgreSQL behavior.** The partial unique index serialized the conflicting
inserts. Lock waits were too short to appear in the polling sample.

**Application behavior.** The pre-check alone was racy, but losing requests
rolled back their failed insert, selected the committed winner, and returned it.
This scenario sent metadata only. If each contender had uploaded an artifact,
the database result would still be one row, but losing contenders could leave
unreferenced files because filesystem writes precede the unique insert.

**Invariant result.** Preserved because the database constraint is
authoritative, not because of the initial Python SELECT.

**Root cause classification.** Expected concurrent behavior correctly handled
by a database uniqueness constraint plus retry/replay code.

Important interleaving:

```text
T1: SELECT key -> none
T2: SELECT key -> none
T1: INSERT key
T2: INSERT same key -> waits/conflicts at unique index
T1: COMMIT
T2: unique violation -> ROLLBACK -> SELECT T1 row -> HTTP 200
```

### Scenario B — concurrent independent submissions

**Design.** One hundred participants simultaneously submit to one challenge,
each with a distinct key.

**Expected.** 100 successful, distinct rows with valid relationships.

**Observed.**

- 100/100 returned 201;
- 100 distinct IDs and rows, 100 distinct participants;
- 0 duplicate creation timestamps;
- p50 2228.72 ms, p95 3728.77 ms, max 3944.93 ms;
- no client errors, pool timeouts, deadlocks, conflicts, or sampled lock waits;
- exact peak app pool checkouts was only 2 (100 total checkouts);
- sampled maximum transaction age was 89.61 ms;
- process RSS peaked at 93.16 MiB; summed PostgreSQL RSS at 322.42 MiB.

**PostgreSQL behavior.** Independent inserts committed without constraint or
lock conflict. The database reached exactly the expected final state.

**Application behavior.** Correct but slow under an instantaneous 100-request
write fanout in this laptop/embedded-PostgreSQL run. Because the client and app
share a process and the pool never approached capacity, this run does not prove
the database pool caused the latency.

**Invariant result.** Foreign keys, uniqueness, timestamps, and row counts were
preserved.

**Root cause classification.** Expected concurrent behavior. Latency is an
observed capacity signal, not yet an attributed bottleneck.

### Scenario C — read/write contention

**Design.** While 60 independent submissions were created, loops read challenge
detail, organizer challenge submissions, and one participant's submissions.

**Expected.** No errors or torn rows. Counts can be stale between requests but
should eventually reach 60.

**Observed.**

- writes: 60/60 returned 201; p50 1275.24 ms, p95 2378.91 ms;
- challenge reads: 8/8 succeeded; p95 1633.30 ms;
- challenge-submission reads: 9/9 succeeded; p95 1466.57 ms;
- participant-submission reads: 7/7 succeeded; p95 1455.29 ms;
- observed challenge list counts: `0, 0, 5, 43, 56, 58, 60, 60, 60`;
- zero decreasing observations and final database count 60;
- no deadlocks, conflicts, or sampled lock waits;
- exact peak pool checkouts 3.

**PostgreSQL behavior.** At `READ COMMITTED`, each SELECT statement saw rows
committed before that statement began. Reads did not block ordinary inserts and
did not see uncommitted rows.

**Application behavior.** Each endpoint returned a separately timed view. The
system offers no cross-endpoint snapshot, so a challenge read and a list read
made at different times need not represent one competition instant.

**Invariant result.** No declared invariant broke. “Strongly consistent
competition view” is not currently defined.

**Root cause classification.** Expected `READ COMMITTED` behavior. High tail
latency is a capacity observation.

### Scenario D — challenge closing race

**Design.** A temporary experiment trigger delayed an actual submission INSERT
for 750 ms after its Python published-state check. While the INSERT was visibly
sleeping in `pg_stat_activity`, direct SQL committed `challenge.status =
'closed'`. The trigger was removed after the scenario.

**Expected question.** Can a submission commit after close?

**Observed answer.** Yes.

- close committed in 10.35 ms;
- the in-flight request returned 201 **757.10 ms after close committed**;
- one row existed for the closed challenge;
- a request started after close returned 404;
- no lock waiter or deadlock was observed.

**PostgreSQL behavior.** Both transactions were valid. The close updated the
challenge row. The submission foreign key later verified that the challenge row
existed; it does not care about status.

**Application behavior.** The request checked status before insert and never
checked again. No lock connected that check to the status update.

**Broken invariant.** If “closed” means no submission may commit after the close
transaction, that invariant is violated. If product semantics are “requests
admitted before close may finish,” the result is acceptable—but that policy is
currently implicit and unrecorded.

**Root cause classification.** Transaction-boundary/check-then-write race plus
a missing database-level or atomic application invariant. Not an isolation
anomaly.

Proven interleaving:

```text
Submission T1: SELECT challenge -> published
Submission T1: SELECT hackathon -> published
Submission T1: INSERT begins; experiment trigger sleeps
Close T2:      UPDATE challenge -> closed
Close T2:      COMMIT
Submission T1: INSERT resumes; FK sees existing challenge
Submission T1: COMMIT
Submission T1: HTTP 201, 757.10 ms after close
```

### Scenario E — lost-response retry

**Design.** Send a request and discard its successful response, then retry. Run
once with the same key and once without a key.

**Observed with key.**

- first response 201, retry 200;
- both responses had the same ID;
- one database row.

**Observed without key.**

- both responses 201;
- IDs differed;
- two additional database rows.

**PostgreSQL behavior.** The partial unique index collapsed only the keyed
requests. Null keys are intentionally excluded.

**Application behavior.** Keyed retry used the fast replay SELECT. Unkeyed retry
is indistinguishable from an intentional new attempt.

**Invariant result.** Preserved when the client supplies a stable key. Duplicate
rows without a key are expected semantics, not a database failure.

**Root cause classification.** Expected idempotent/non-idempotent behavior.

### Scenario F — concurrent terminal transitions

This additional scenario tests the stated submission state-machine invariant.

**Design.** Simultaneously call submit and cancel on one `created` submission.

**Observed.**

- submit returned 200 and reported `submitted`;
- cancel returned 200 and reported `cancelled`;
- final database state was `submitted`;
- both terminal transitions were accepted.

**PostgreSQL behavior.** Row updates serialized, so no physical corruption
occurred. The later update overwrote the earlier status. The status check
constraint accepted both values.

**Application behavior.** Both transactions read `created` before either
terminal write became visible. Both Python checks passed. Because
`expire_on_commit=False`, each request reported its own session's value even
though one was later overwritten.

**Broken invariant.** The externally observed history says both mutually
exclusive terminal transitions succeeded. The declared state machine says a
terminal state cannot transition again.

**Root cause classification.** Lost-update race caused by application-only
validation and an unconditional UPDATE at `READ COMMITTED`.

Interleaving consistent with the evidence:

```text
Cancel T1: SELECT submission -> created
Submit T2: SELECT submission -> created
Cancel T1: Python allows created -> cancelled
Submit T2: Python allows created -> submitted
Cancel T1: UPDATE cancelled; COMMIT; HTTP 200 cancelled
Submit T2: UPDATE submitted; COMMIT; HTTP 200 submitted
Final row: submitted
```

## Part 4: PostgreSQL observations

Across the run:

- PostgreSQL version 16.2, isolation `read committed`;
- no deadlocks;
- no `pg_stat_database` conflicts;
- no lock wait was caught by the 20 ms target polling loop;
- maximum sampled database connections: 7;
- exact app pool peak: 3 during mixed read/write contention, 2 during the
  100-write burst;
- no pool timeout;
- longest sampled transaction: 779.27 ms, intentionally caused by Scenario D's
  sleep trigger;
- maximum summed PostgreSQL RSS: 325.62 MiB;
- maximum sampled database CPU: 33.4%;
- process CPU peaked above 100% because psutil reports multi-core use and the
  same process ran both load client and application.

Absence of a sampled lock wait does not prove no micro-wait occurred. In
particular, Scenario A necessarily coordinated the unique-key conflict inside
PostgreSQL, but the wait was shorter than observation resolution.

## Part 6: no fixes applied

The experiment did not add a close endpoint, row locks, version columns,
conditional updates, stronger isolation, Redis, queues, distributed locks, or
services. The temporary PostgreSQL trigger existed only inside Scenario D and
was dropped in `finally`.

## Part 7: ACID through ChallengeForge

### Atomicity

Atomicity worked for PostgreSQL state. A successful submission row committed as
one unit; a duplicate-key loser rolled back before reading the winner. Scenario
A did not leave a half-inserted second row.

Atomicity does **not** automatically include the filesystem. Artifact bytes are
written before the database commit, so a failed transaction can leave an
orphan. That path was outside this run but remains an application transaction
boundary.

### Consistency

Database consistency means every committed row satisfies declared foreign keys,
non-null rules, check constraints, and unique indexes. PostgreSQL maintained all
of those.

Application/business consistency is stronger:

- only published challenges accept submissions;
- exactly one terminal transition wins and only that one reports success;
- “closed” has a defined temporal meaning.

Scenarios D and F show that database consistency does not imply those business
rules. They are not fully represented in the atomic database operation.

### Isolation

`READ COMMITTED` isolated each statement from uncommitted data. Scenario C
readers saw only committed prefixes (`0 -> 5 -> 43 -> ... -> 60`), while writes
continued.

It did not make a multi-statement check-then-write sequence indivisible.
Scenario D's earlier status SELECT did not reserve “published” until insert.
Scenario F's earlier state SELECT did not reserve `created` until update.

### Durability

Once the first keyed submission committed, a later lost-response retry found
the same durable row and returned it. Durability is what makes idempotency
recovery possible after the client loses the response.

This run did not simulate process crash, OS crash, disk loss, or PostgreSQL
fsync configuration, so it validates retry-visible committed state—not disaster
recovery.

## Part 8: architectural questions earned by the experiment

Do not answer these by adding infrastructure yet:

1. Does “challenge closed” reject requests by request-arrival time, validation
   time, insert time, or commit time?
2. Should an admitted-before-close submission be allowed to finish?
3. What single atomic database statement or concurrency policy should encode a
   terminal submission transition?
4. Should conflicting terminal requests return one success and one 409?
5. Is idempotency scoped to participant, participant+challenge, or a named
   operation?
6. Should reuse of a key with a different payload be replayed or rejected?
7. What defines strict submission ordering when timestamps tie?
8. Do list endpoints need cursor pagination and a stable `(created_at, id)`
   order?
9. Which UI/API views need one competition-wide snapshot, and which tolerate
   `READ COMMITTED` freshness?
10. Will user roles become mutable, and if so where is “participant at submit
    time” enforced?
11. Which invariants belong in constraints, conditional UPDATE/INSERT
    statements, row locks, or optimistic version checks?
12. What is the sustained throughput and p95 latency on the actual target
    laptop with an external PostgreSQL process and separate load generator?
13. Why did this run peak at only three checked-out app connections despite a
    large client fanout: short transactions, event-loop scheduling, or another
    bottleneck?
14. At what measured evaluation/AI duration does request-response processing
    stop being appropriate and earn a queue?
15. When a leaderboard exists, is ranking computed from committed scores on
    demand, transactionally materialized, or allowed to lag?

