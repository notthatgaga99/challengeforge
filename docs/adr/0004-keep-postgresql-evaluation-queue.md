# ADR 0004: Keep PostgreSQL as the evaluation job queue (for now)

## Status

Accepted.

## Context

After introducing the async evaluation pipeline, a burst of 100 queued jobs
showed queue wait (seconds) dominating execution time (tens of milliseconds).
The naive reaction would be “add Redis/Kafka.”

Instead we measured arrival rate, service rate, worker scaling, burst drain,
polling cost, fairness, and failure recovery on this laptop configuration
(`docs/evaluation-capacity-report.md`, `docs/evaluation-capacity-results.json`).

## Decision

**Keep PostgreSQL (`FOR UPDATE SKIP LOCKED`) as the evaluation queue.**

Do **not** introduce Redis, Kafka, RabbitMQ, Celery, or Temporal in this
iteration.

## Evidence

1. **Below capacity (~1–5 arrivals/s with 4 workers):** queue trend stayed
   stable; wait p50 stayed under ~200 ms for low rates.
2. **Above capacity:** queue depth grew and wait rose into seconds — the
   expected backlog behavior — without losing durable submissions.
3. **Burst of 400:** all submissions were accepted (`all_submitted=true`) while
   evaluation backlog peaked hundreds deep. Submission correctness remained
   independent of evaluation completion.
4. **Polling cost:** empty-queue polling is real (on the order of
   `1 / poll_interval` queries per worker) but was not the binding limit
   compared with queue wait under saturation.
5. **Fairness:** global FIFO delayed a second challenge behind a large first
   backlog — a scheduling concern, not a queue-storage concern.
6. **Failure injection:** stale RUNNING recovery and evaluator failure behaved
   as designed; no duplicate successful completion observed.

## Backpressure

Evaluation capacity exhaustion is surfaced as `normal|busy|saturated|critical`
messaging (`GET /api/v1/evaluations/backlog`, submit response fields).
Submissions are **not** rejected for evaluation backlog reasons.

## Consequences

**Positive**

- One operational store; correctness already proven in PostgreSQL.
- Capacity and backlog are measurable with existing timestamps.

**Negative / accepted debt**

- Empty-queue polling wastes queries.
- Global FIFO can starve later challenges.
- Throughput ceiling under this workload is modest relative to theoretical
  `workers / fake_work` because claim/DB overhead dominates the 50 ms sleep.

## When to revisit

Reconsider a dedicated queue when measured evidence shows one of:

- empty-queue polling or connection pressure becomes a first-order cost;
- multi-tenant fairness requires partitioned scheduling that fights PostgreSQL
  claim patterns;
- sustained arrival rates that this monolith+Postgres cannot drain without
  unacceptable wait *after* worker/process tuning.

Until then, infrastructure replacement would be technology-driven, not
evidence-driven.

## Related

- [evaluation-capacity-report.md](../evaluation-capacity-report.md)
- [evaluation-pipeline.md](../evaluation-pipeline.md)
- ADR 0003 (async evaluation boundary)
