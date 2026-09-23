# Ingestion Pipeline Reliability

**Status:** Measured — **KEEP PostgreSQL-backed durable ingestion jobs**  
**ADR:** `docs/adr/0010-ingestion-pipeline.md`  
**Harness:** `scripts/ingestion_pipeline_reliability_experiment.py`  
**Results:** `docs/ingestion-pipeline-reliability-results.json`  
**Code:** `challengeforge.ingestion`, `ingestion_jobs` table  
**Tests:** `tests/test_ingestion_pipeline.py`

> **At-least-once execution ≠ exactly-once effects.**  
> Effects are made safe by stable `result_key` overwrite + conditional completion.

Chunking / embeddings / RAG are **out of scope**.

---

## Problem

Committed artifacts had no async processing plane. Before PARSE / NORMALIZE /
index stages, ChallengeForge needs durable job semantics: no silent loss,
bounded retries, and safe worker-death recovery — without assuming Redis,
Kafka, Celery, or Temporal.

## Ingestion contract

```text
COMMITTED ARTIFACT (artifact_key on submission)
        ↓  enqueue ingestion_jobs (same UoW as attach)
      PARSE
        ↓
    NORMALIZE
        ↓
   READY output at ingestion/{job_id}/result.json
        ↓
   job status = succeeded (+ result_key)
```

| Aspect | Definition |
|---|---|
| Valid job input | Durable `artifact_key` already committed + `ingestion_jobs` row |
| Successful output | Blob at `ingestion/{job_id}/result.json` **and** job `succeeded` with matching `result_key` |
| Progress durability | Postgres job state (`queued` / `running` / `succeeded` / `failed`) |
| Failure | Terminal `failed` (deterministic / exhausted) or requeue `queued` (transient) |
| Idempotency | Same `result_key` overwritten; `mark_succeeded` only if still RUNNING for claiming worker |

**Invariant:** A committed artifact is never silently dropped from ingestion, and
a job is never marked `succeeded` without a durable `result_key`.

## Failure model

| Scenario | Observed DB outcome |
|---|---|
| Job created, worker never starts | Remains `queued`; artifact untouched |
| Worker claims then dies | Stays `running` until stale recovery → requeue or `failed` |
| Dies halfway through processing | Stale recovery; partial result cleaned; reprocess |
| Process OK, ack fails | Result blob may exist; stale requeue; overwrite + conditional succeed |
| Duplicate delivery | Second complete is no-op if ownership lost; result key stable |
| Deterministic malformed | Immediate `failed` (`malformed_artifact` / encoding / missing) |
| Transient processing | `queued` with `attempt_count` until success or max |
| Downstream / unknown error | Bounded retry then terminal `failed` |
| Repeated crash | Attempts increment; eventually `failed` (`stale_exhausted`) |
| Process restart | Durable rows survive; workers resume claim / recover |
| DB unavailable mid-transition | Transaction rolls back; job stays prior durable state |

## State machine

```text
QUEUED  → RUNNING     (claim: FOR UPDATE SKIP LOCKED)
RUNNING → SUCCEEDED   (conditional on worker_id)
RUNNING → FAILED      (deterministic or attempts exhausted)
RUNNING → QUEUED      (transient retry or stale recovery)
```

No separate `RETRYABLE_FAILURE` state: retry scheduling is `queued` +
`available_at` + `attempt_count` + `error_code`.

**Fields (minimum demonstrated need):**  
`id`, `submission_id`, `artifact_key`, `status`, `attempt_count`,
`worker_id`, `available_at`, `started_at`, `completed_at`, `error_code`,
`error_message`, `result_key`, `created_at`, `updated_at`.

**Stale threshold:** `ingestion_stale_after_seconds` (default 300s; tests use 0).

## Idempotency

Target: **at-least-once execution + idempotent effects**.

Worker dies after writing `ingestion/{job_id}/result.json` but before
`mark_succeeded` → stale recovery requeues → cleanup may delete prior blob →
re-process overwrites the same key → conditional succeed. No conflicting
durable “ready” without a successful job row.

Exactly-once *execution* is not claimed and not required.

## Retry semantics

| Class | Examples | Behavior |
|---|---|---|
| Deterministic | poison, invalid UTF-8, missing blob | Terminal `failed` immediately |
| Transient | synthetic backend blip, worker interrupt | Requeue until `ingestion_max_attempts` |
| Unknown / infra | unexpected exception, stale claim | Bounded retry then terminal |

**Backoff:** default `ingestion_retry_delay_seconds = 0` (immediate). Workload is
short synthetic parse/normalize; contention was not observed. Add delay only if
hot-looping on a flapping dependency.

## Worker death

`recover_stale_running` mirrors evaluation recovery **without** process-tree
ownership (ingestion does not spawn participant processes).

- Stale `RUNNING` + attempts remaining → `QUEUED`, clear `worker_id`, cleanup partial result  
- Attempts exhausted → `FAILED` (`stale_exhausted`)  
- Recovery must not produce two conflicting durable ready outputs (stable key + conditional complete)

## Poison inputs

Synthetic prefix `cf_ingest_poison…` → terminal FAILED. SKIP LOCKED claim means
one poison job does **not** block unrelated work (measured: 10 succeeded + 1
failed).

User visibility today: job `error_code` / `error_message` in DB (API surfacing
can follow later).

## Concurrency

`FOR UPDATE SKIP LOCKED` was selected because concurrent claim races are real
once >1 worker runs (same pattern as evaluation). Measured laptop cells:

| Workers | Jobs | Wall (s) | jobs/s | Outcome |
|---|---|---|---|---|
| 1 | 20 | 2.84 | 7.05 | 20 succeeded |
| 2 | 50 | 8.73 | 5.73 | 50 succeeded |
| 4 | 100 | 15.30 | 6.54 | 100 succeeded |

Duplicate success count across workers was not observed (tests + experiment).

## Fairness / starvation

FIFO by `created_at` is sufficient for current synthetic work. Poison jobs
terminate without infinite retry. Expensive real parse stages may later cause
head-of-line blocking — revisit then; do not build a general scheduler now.

## Backpressure

Ingestion queue growth does **not** reject committed artifacts. Upload path
remains independent. If backlog threatens interactive SLOs, expose depth /
degrade optional processing — do not delete committed blobs.

## Queue design

Reuse evaluation lessons (`claim` / stale / conditional complete), but a
**separate** `ingestion_jobs` table: different payload, no score/workload class,
no process ownership. Not a second queue abstraction library.

## Capacity envelope (dev machine)

Sustainable ≈ **6–9 jobs/s** for this synthetic stage on the experiment host
(not a production SLO). Queue wait is dominated by poll interval when idle.
Processing is sub-second for tiny docs. Failure/retry rates ≈ 0 under healthy
load; poison cell: 1 failed, 0 infinite retries. Failure-injection cells all
`ok` in `docs/ingestion-pipeline-reliability-results.json`.

## DB queue vs external queue

| Criterion | Postgres jobs | External queue |
|---|---|---|
| Durability | Same DB as artifacts metadata | Extra system |
| Ops | Already deployed | Redis/Kafka/Celery ops |
| Measured need | ~9 jobs/s synthetic | Not justified |
| Multi-region / fan-out | Weak | Stronger later |

**Decision:** PostgreSQL is sufficient at current scale.

## Decision

### KEEP PostgreSQL-backed durable ingestion jobs

Enqueue on artifact attach; worker claim / process / complete; stale recovery;
deterministic vs transient classification; idempotent result overwrite.

## Correctness invariants (verified)

1. Committed artifacts enqueue a job (not silently lost).  
2. Jobs survive worker death (row durable).  
3. Duplicate delivery does not corrupt ready output identity.  
4. Deterministic failures do not retry forever.  
5. Transient failures can recover.  
6. Stale claims recover safely.  
7. One poison job does not block the pipeline.  
8. Success is durably visible (`succeeded` + `result_key` exists).  
9. Partial outputs cleaned on retry paths; ready requires both blob + status.  
10. Upload / commit path is not gated on ingestion backlog.

## What evidence proves / does not prove

**Proves (this laptop / synthetic stage):** durability semantics, claim races,
poison isolation, stale recovery idempotency, rough throughput.

**Does not prove:** production capacity, multi-host workers, real PDF/parser
cost, RAG indexing SLOs, object-store backends.

## Reconsideration triggers

Jobs/sec exceed Postgres polling comfort · multi-region consumers · multi-step
RAG workflows needing richer orchestration · ingestion backlog harming
interactive API latency · need for cross-service fan-out.
