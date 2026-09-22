# Evaluation pipeline — first asynchronous lifecycle

## Purpose

Turn **submission** from a purely synchronous database operation into a durable
workflow where acceptance and scoring are separate:

1. Participant submits → submission becomes `SUBMITTED`, evaluation is `QUEUED`.
2. A worker claims the evaluation → `RUNNING`.
3. A deterministic placeholder evaluator finishes → `SUCCEEDED` or `FAILED`.

This iteration deliberately uses **PostgreSQL as the job queue**. No Redis,
Kafka, Celery, Temporal, or other job platforms.

---

## 1. Evaluation state machine

```
QUEUED ──claim──► RUNNING ──► SUCCEEDED
                     │
                     ├──► FAILED          (evaluator failure — terminal)
                     │
                     └──► QUEUED          (stale RUNNING recovery, under max attempts)
```

Database check constraint:

`status IN ('queued', 'running', 'succeeded', 'failed')`

Domain-allowed transitions are encoded in
`challengeforge.domain.evaluation_state`.

---

## 2. Uniqueness invariant

**One evaluation row per submission.**

Enforced by `UNIQUE (submission_id)` on `evaluations`.

Retries of submit (lost HTTP response) must return the existing evaluation, not
insert a second row.

---

## 3. Transaction boundaries

### Submit + enqueue (API request)

Single PostgreSQL transaction:

1. Conditional `UPDATE submissions SET status='submitted' WHERE status='created' …`
2. If that UPDATE won: `INSERT INTO evaluations (…, status='queued')`
3. `COMMIT`

**Chosen atomicity:** submission acceptance and evaluation creation commit
together. The system cannot observe `SUBMITTED` without a matching evaluation,
and cannot observe an evaluation whose submission insert/update rolled back.

Cancel does **not** create an evaluation.

### Worker claim

Separate short transaction:

1. `SELECT … FROM evaluations WHERE status='queued' ORDER BY created_at ASC, id ASC FOR UPDATE SKIP LOCKED LIMIT 1`
2. Mutate row → `running`, bump `attempt_count`, set `started_at` / `worker_id`
3. `COMMIT`

Evaluation work then runs **outside** that transaction so a long evaluator does
not hold row locks. Completion is another conditional UPDATE requiring
`status='running' AND worker_id=<claimer>`.

### Crash recovery

Separate transaction run by each worker loop:

- Stale `RUNNING` with `started_at < now() - stale_after` and
  `attempt_count < max_attempts` → `QUEUED`
- Same stale predicate with `attempt_count >= max_attempts` → terminal `FAILED`

---

## 4. Job claiming mechanism

**Mechanism:** `SELECT … FOR UPDATE SKIP LOCKED` then in-session transition to
`RUNNING`.

**Why:** two worker processes may poll the same queue. `SKIP LOCKED` lets each
worker take a different row without waiting on a peer’s claim transaction.
A plain `SELECT` then later `UPDATE` would allow two workers to read the same
`QUEUED` row before either commits.

Completion UPDATEs are also conditional on `worker_id` so a recovered job
cannot be finished by a crashed worker that wakes up late.

---

## 5. Worker lifecycle

Entry point:

```text
python -m challengeforge.worker --worker-id worker-a
```

Loop:

1. Recover stale RUNNING rows
2. Claim one QUEUED evaluation (or sleep)
3. Load submission, run deterministic placeholder evaluator
4. Mark SUCCEEDED or FAILED

Multiple OS processes may run concurrently against the same database.

---

## 6. Retry semantics (this iteration)

| Failure mode | Policy |
|---|---|
| Placeholder evaluator raises (`force_evaluation_failure`) | Immediate terminal **FAILED**. No automatic retry. |
| Worker crash while RUNNING | After `evaluation_stale_after_seconds` (default 30), requeue to **QUEUED** if `attempt_count < evaluation_max_attempts` (default 3). |
| Exhausted attempts after stale recovery | Terminal **FAILED** with an abandonment reason. |

**Decision:** deliberate evaluator failure is terminal; crash/loss of the worker
is retryable up to a small attempt budget. We are learning these semantics
before building a sophisticated retry engine.

---

## 7. Crash recovery

| Question | Answer |
|---|---|
| What is stale? | `status=running` AND `started_at` older than `evaluation_stale_after_seconds` |
| Who detects? | Any evaluation worker on each loop iteration |
| How requeued? | Conditional UPDATE → `queued`, clear `worker_id` / `started_at` |
| Max attempts | `evaluation_max_attempts` (default 3); counted on each successful claim |
| After max | Terminal `FAILED` |

---

## 8. Idempotency relationship

Submission create still uses the existing participant + `Idempotency-Key` unique
index and request fingerprint.

Submit:

- First successful transition creates exactly one evaluation in the same commit.
- A retry after success returns the existing `SUBMITTED` submission and its
  evaluation (`UNIQUE submission_id` prevents a duplicate insert).

---

## 9. Failure behavior

- Participant sees `GET /api/v1/submissions/{id}/evaluation` with
  `status=failed`, `score=null`, `submission_accepted=true`, and a friendly
  `failure_message` (not raw internal machinery).
- The submission itself remains `submitted` — evaluation failure is separate.
- No phantom `succeeded` row: completion requires a conditional UPDATE from
  `running`.

---

## 10. Queue wait vs execution time

Persisted timestamps support:

| Metric | Formula |
|---|---|
| `queue_wait_time` | `started_at - created_at` |
| `execution_time` | `completed_at - started_at` |
| `end_to_end_evaluation_time` | `completed_at - created_at` |

These are reported separately in the adversarial experiment so saturation
(queue wait) is not confused with evaluator cost (execution).

---

## 11. API

| Endpoint | Behavior |
|---|---|
| `POST …/submissions/{id}/submit` | Accepts submission; response includes `evaluation_id`, `evaluation_status=queued`, `evaluation_async=true`, and messaging that evaluation is asynchronous. Does **not** wait for scoring. |
| `GET …/submissions/{id}/evaluation` | Participant view: status, queue `position` when queued, optional `estimated_wait_seconds`, score / failure message when terminal. |
| `GET …/organizer/evaluation-queue` | Organizer aggregate: queued / running / completed / failed / oldest queued age. |
| `GET …/evaluations/backlog` | Health messaging for busy/saturated states (submissions still accepted). |

---

## FIFO semantics

**Selection order** for queued work:

```text
ORDER BY created_at ASC, id ASC
```

- `created_at` prefers older work.
- `id` breaks ties deterministically when timestamps collide.

This is **FIFO selection**, not strict FIFO **completion**:

- Multiple workers may run jobs concurrently.
- A later-started job can finish before an earlier one that is still RUNNING.
- What we guarantee: when choosing the next *available* `QUEUED` row (unlocked),
  the oldest `(created_at, id)` wins.

`SKIP LOCKED` preserves that selection rule under concurrency without waiting on
another worker’s claim transaction.

---

## Participant experience

| State | What the participant sees |
|---|---|
| QUEUED | Submission received; evaluation queued; optional position and estimated wait |
| RUNNING | Submission received; evaluation in progress |
| SUCCEEDED | Submission received; score and completion time |
| FAILED | Submission received; evaluation failed with a clear message — submission still accepted |

### Queue position

Among **currently QUEUED** evaluations only, using the same FIFO order:

- `position = 1` means next in line to be claimed.
- RUNNING jobs are **not** counted ahead (they already left the selectable queue).
- Position is a **point-in-time** view: claims, failures, and recovery can change
  it between polls. It is informational, not a lock or promise.

### Estimated wait

When recent completion throughput is available (≥ 3 completions in the service
rate window):

`estimated_wait_seconds ≈ position / recent_service_rate`

Otherwise the API returns `null` rather than inventing a number.

**Estimated wait is informational and is not a completion-time guarantee.**

The submission page polls every ~2.5s while status is QUEUED or RUNNING, and
stops on SUCCEEDED / FAILED. Polling only GETs evaluation status — it never
creates submissions or evaluations.

---

## Consistency vs information

| Strong correctness (must not race) | Informational / approximate |
|---|---|
| Submission terminal transitions | Queue position |
| Challenge close vs acceptance | Estimated wait |
| Idempotency + fingerprints | Aggregate queue counts |
| One evaluation per submission | Backlog health labels |
| Atomic claim + worker ownership | Oldest-queued age display |
| Crash recovery / max attempts | |

Do not lock the entire queue merely to freeze a position for a browser poll.

---

## 12. Known limitations

- PostgreSQL polling is a teaching primitive, not a production job system.
- Stale recovery is time-based and approximate; a slow legitimate evaluation that
  exceeds the stale threshold can be requeued (hence conditional completion by
  `worker_id`).
- Global FIFO can delay later challenges behind an earlier backlog (see capacity
  report) — per-challenge fairness is out of scope here.
- No priority queues, WebSockets, or sophisticated wait prediction.
- Evaluator does not execute participant code.
- Worker is a separate process sharing the modular monolith’s domain/persistence
  layers — not a microservice.

---

## Related

- ADR: [adr/0003-async-evaluation-boundary.md](adr/0003-async-evaluation-boundary.md)
- Capacity: [evaluation-capacity-report.md](evaluation-capacity-report.md)
- Experiment: `python scripts/evaluation_experiment.py`
- Results: [evaluation-pipeline-results.json](evaluation-pipeline-results.json)
