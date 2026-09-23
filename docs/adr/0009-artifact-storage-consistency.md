# ADR 0009: Artifact storage consistency

## Status

Accepted: **BLOB-FIRST + COMPENSATING DELETE + RECONCILIATION**

## Context

Submission artifacts are stored outside PostgreSQL via `ArtifactStorage`.
Historically the application wrote the blob, then committed `artifact_key`.
If the commit failed, an orphaned blob remained. Architecture accepted orphans
and forbade dangling DB references. There is no async ingestion pipeline yet.

## Decision

1. Keep **blob-then-metadata** write order (never publish a key without bytes).  
2. On metadata transaction failure, **compensating `delete`** of the new key.  
3. On successful replace, best-effort delete of the previous key.  
4. Provide **`reconcile_orphans`** with a grace window for in-flight uploads.  
5. Add `delete` / `iter_keys` / `mtime` to the storage protocol.  
6. Do **not** introduce object storage, DB BYTEA, CAS, or a durable artifact
   status enum until ingestion or scale evidence requires them.

## Alternatives

| Option | Why not now |
|---|---|
| Metadata-first + UPLOADING | Extra states; dangling-key risk |
| Outbox / rich state machine | No ingestion consumer yet |
| Content-addressed store | Dedup nice-to-have |
| PostgreSQL BYTEA | Wrong size/scale trade-off |
| External object storage | Same dual-write problem; ops cost |

## Evidence

- `docs/artifact-storage-consistency.md`  
- `scripts/artifact_storage_consistency_experiment.py`  
- `tests/test_artifact_consistency.py`

## Trade-offs

**+** Closes measured orphan gap; preserves no-dangling-key invariant; small diff  
**−** Compensate is best-effort; grace window must be tuned; no content dedupe

## Reconsideration

Persistent orphan growth · need virus-scan/unpack pipeline · multi-node blob
backend · storage cost dominated by duplicates.

## Related

ADR 0001 (modular monolith / storage boundary), architecture § Artifact-storage.
