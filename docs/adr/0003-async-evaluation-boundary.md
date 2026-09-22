# ADR 0003: Asynchronous evaluation boundary

## Status

Accepted.

## Context

Vertical Slice 1 treated submission as a synchronous database write. After
acceptance, there was nowhere for scoring to live. Real evaluation (even a
placeholder) takes wall-clock time and must not hold the participant’s HTTP
request open.

We already proved PostgreSQL-authoritative correctness for terminal submission
transitions, close-vs-accept, and idempotency. Those mechanisms must stay
intact. The next product step is the **smallest durable lifecycle** that
separates “accepted” from “scored.”

## Decision

1. Keep ChallengeForge a modular monolith.
2. On successful `CREATED → SUBMITTED`, create exactly one `evaluations` row in
   `QUEUED` inside the **same PostgreSQL transaction**.
3. Run evaluation in a separate worker process that claims jobs with
   `SELECT … FOR UPDATE SKIP LOCKED`.
4. Do **not** introduce Redis, Kafka, Celery, Temporal, or distributed locks yet.
5. Expose evaluation status over HTTP; never block submit on evaluator completion.

## Why evaluation left the HTTP request

- Submission acceptance is a competition correctness concern (close races,
  idempotency). Scoring is a different concern with different failure modes.
- Holding the request until scoring finishes couples client timeouts to worker
  load and teaches the wrong reliability model.
- Persisting `QUEUED` work makes retries, crashes, and multiple workers
  observable without inventing an external queue first.

## Consequences

**Positive**

- Every accepted submission has a durable evaluation record.
- Multiple workers can share one database safely.
- Queue wait and execution time are measurable separately.

**Negative / accepted debt**

- Polling PostgreSQL does not scale like a purpose-built queue.
- Time-based stale recovery is crude.
- Operators must run at least one worker process for scores to appear.

## Alternatives considered

| Alternative | Why not now |
|---|---|
| Score inside the submit request | Couples latency; no crash recovery story |
| Redis / Celery / Temporal | Solves distribution before we understand our own lifecycle |
| Outbox + message broker | Correct pattern later; too much machinery for the first lesson |

## Related

- [evaluation-pipeline.md](../evaluation-pipeline.md)
- ADR 0002 (database-authoritative concurrency) remains in force for submissions
