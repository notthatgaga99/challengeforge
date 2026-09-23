# ADR 0010: Ingestion pipeline durability

## Status

Accepted: **PostgreSQL-backed durable ingestion jobs** (no external queue yet).

## Context

Artifacts are committed via blob-first upload, but nothing processed them
asynchronously. Before PARSE/NORMALIZE/index/RAG stages, ChallengeForge needs
durable job semantics, retries, and worker-death recovery.

## Decision

1. Add `ingestion_jobs` with `queued|running|succeeded|failed`.  
2. Enqueue a job when an artifact is durably attached.  
3. Workers claim with `FOR UPDATE SKIP LOCKED`.  
4. Synthetic processor writes idempotent `ingestion/{job_id}/result.json`.  
5. Deterministic failures are terminal; transient/unknown retry until max attempts.  
6. Stale RUNNING recovery requeues or fails by attempt count.  
7. Do **not** add Redis/Kafka/Celery/Temporal absent evidence.

## Alternatives

| Option | Why not |
|---|---|
| Sync ingest in upload request | Couples latency; loses work on request death |
| In-process background task | Not durable across process restart |
| External queue | Ops cost; not needed at measured scale |
| Workflow engine | Overkill for one stage pipeline |

## Evidence

- `docs/ingestion-pipeline-reliability.md`  
- `scripts/ingestion_pipeline_reliability_experiment.py`  
- `tests/test_ingestion_pipeline.py`  
- Migration `0011_ingestion_jobs`

## Trade-offs

**+** Reuses proven Postgres patterns; durable; concurrent; small  
**−** Polling latency; not multi-region; effects “exactly once” only via idempotency

## Reconsideration

Sustained high jobs/sec · cross-host workers needing a bus · multi-step RAG
workflows · ingestion backlog harming interactive SLOs.

## Related

ADR 0003/0004 (evaluation queue), ADR 0009 (artifact consistency).
