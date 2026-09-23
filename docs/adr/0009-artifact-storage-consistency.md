# ADR 0009: Artifact storage consistency and upload boundary

## Status

Accepted:

1. **BLOB-FIRST + COMPENSATING DELETE + RECONCILIATION**  
2. **KEEP + temporary/finalize streaming upload** (not resumable, not S3)

## Context

Artifact bytes live outside PostgreSQL. Dual-write orphans were closed with
compensating delete and grace-aware reconciliation. Separately, the HTTP attach
path buffered the entire multipart body in memory (`await file.read()`), so peak
RSS tracked artifact size.

## Decision

1. Keep blob-then-metadata; compensate on commit failure; reconcile orphans.  
2. Stream request bodies to `.incoming/{uuid}`, hash optionally (`X-Content-SHA256`),
   then atomically `put_from_path` to the final key before committing metadata.  
3. Reclaim abandoned incoming files via cleanup (opportunistic + reconcile).  
4. Do **not** add chunked/resumable protocols or object storage yet.  
5. Do **not** treat checksum as content-addressed deduplication.

## Alternatives

| Option | Why not now |
|---|---|
| Keep full-body buffer | RSS scales with size; unnecessary |
| Resumable / chunked uploads | No evidence at ≤5 MiB default |
| Direct object-storage upload | Same consistency problem; ops cost |
| CAS keys | Dedup not required yet |

## Evidence

- `docs/artifact-storage-consistency.md`  
- `docs/large-upload-architecture.md`  
- `scripts/large_upload_architecture_experiment.py`  
- `tests/test_large_upload.py` / `tests/test_artifact_consistency.py`

## Trade-offs

**+** Bounded memory; complete-only finals; small API change  
**−** Extra disk staging; whole-upload retry only; best-effort incoming GC

## Reconsideration

Larger max size · unreliable networks · interactive degradation under upload
pressure · shared multi-node storage · dedupe / ingestion pipeline needs.

## Related

ADR 0001; architecture § Artifact-storage.
