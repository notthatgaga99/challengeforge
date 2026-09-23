# Artifact Storage & Ingestion Consistency

**Status:** Measured — **BLOB-FIRST + COMPENSATING DELETE + RECONCILIATION**  
**ADR:** `docs/adr/0009-artifact-storage-consistency.md`  
**Harness:** `scripts/artifact_storage_consistency_experiment.py`  
**Results:** `docs/artifact-storage-consistency-results.json`  
**Code:** `challengeforge.application.artifacts`, `storage.filesystem`

---

## Problem

Blob write and Postgres metadata are not one atomic transaction. Historically:

```text
storage.put(key)
session.commit()  # may fail → orphaned blob
```

Architecture previously accepted orphans and forbade dangling DB keys. This
milestone measures that gap and closes it with the **smallest** mechanism that
preserves the invariant without object storage or a full artifact state machine.

There is **no async ingestion plane** today. Evaluation fingerprints
`artifact_key` and does not read bytes. “Ingestion” in the threat matrix is
future work that must key off **committed** metadata only.

---

## Consistency invariant

> A product-visible artifact **exists** iff a durable submission row references
> `artifact_key` **and** the blob is present in storage.

| Condition | Classification |
|---|---|
| key in DB + blob present | **exists** (authoritative) |
| blob present, no DB reference | **orphan** (garbage) |
| key in DB, blob missing | **integrity failure** (must not introduce) |

---

## Current model (after this change)

| Concern | Behavior |
|---|---|
| Identity | Opaque key `submissions/{challenge}/{participant}/{uuid}[ext]` |
| Metadata | `submissions.artifact_key` (nullable Text) |
| Blob | `{artifact_root}/…` + `.content_type` sidecar |
| Ownership | Submission participant; organizer read via challenge |
| Upload | `POST …/artifact` (CREATED only) or create-with-artifact (app path) |
| Write order | **Blob first**, then metadata commit |
| On commit fail | **Compensating `delete`** of the new key |
| On replace | After successful commit, delete previous key |
| Deletion API | Storage `delete`; no user-facing artifact DELETE yet |
| Ingestion | None — evaluation must not assume bytes until key committed |
| Idempotency | Create fingerprint excludes artifact; attach not idempotent |
| Reconciliation | `reconcile_orphans` with grace window |

```text
HTTP
  ↓
storage.put(new_key)          ← durable bytes
  ↓
Postgres commit(artifact_key) ← durable reference
  ↓
delete(old_key) best-effort   ← replace GC
  ↓
(future) ingestion of committed keys only
```

---

## Failure matrix

| Failure | Blob | DB | Desired / observed |
|---|---|---|---|
| put fails | none | unchanged | no dangling key ✓ |
| put OK, commit fails | written → **compensate delete** | rolled back | orphan closed ✓ |
| put OK, commit OK | present | referenced | exists ✓ |
| request dies mid-put | partial/.tmp cleaned by replace semantics | unchanged | no publish ✓ |
| duplicate / replace attach | new put; old deleted after commit | points to new | previous GC'd ✓ |
| same content twice | two keys (no CAS) | two refs if two rows | storage cost; deferred dedupe |
| delete metadata only | orphan until reconcile | gone | reconcile ✓ |
| ingestion before durable | N/A today | — | **forbidden by contract** |
| worker death mid-ingest | N/A today | — | future: attempt identity |
| reconcile twice | no-op second time | — | idempotent ✓ |

---

## Design space

| Option | Consistency | Complexity | Chosen? |
|---|---|---|---|
| **A blob-first + reconciliation** | Strong vs dangling keys; orphans GC'd | Low | **Yes (+ compensate)** |
| B metadata-first + pending upload | Needs UPLOADING state; risk of dangling | Med | No |
| C outbox / rich state machine | Stronger workflows | High | Not justified (no ingestion) |
| D content-addressed | Dedup | Med | Deferred |
| E DB BYTEA | Single TX | Poor scale | No |
| F object storage + DB | Same dual-write problem | Ops | Not required yet |
| G hybrid | — | High | Later |

---

## Lifecycle

No new durable enum. Logical phases only:

```text
(none) → bytes on disk → referenced by submission → [future ingest] → GC
```

READY is implied by `artifact_key IS NOT NULL` + `storage.exists(key)`.

---

## Reconciliation

`reconcile_orphans(storage, referenced_keys, grace_seconds=…)`:

1. Scan keys under `submissions/`  
2. Skip keys still referenced  
3. Skip orphans younger than grace (in-flight put→commit)  
4. Delete the rest  
5. Report referenced-but-missing as integrity signals  

Idempotent. Safe to run periodically.

---

## Decision

### BLOB-FIRST + COMPENSATING DELETE + RECONCILIATION

Keep blob-before-metadata. Close the demonstrated orphan path with immediate
compensating delete and a grace-aware reconciler. Do **not** add S3, BYTEA, or
an artifact status table until ingestion or multi-backend storage requires them.

---

## What we prove / do not prove

**Prove:** orphan on commit failure is reproducible; compensation removes it;
reconcile deletes unreferenced keys and skips young ones; replace GC works.

**Do not prove:** object-store semantics; CAS dedupe savings; a full ingestion
pipeline; multi-region consistency.

## Reconsideration

- Orphan volume still grows (compensate failures) → stronger two-phase  
- Need dedupe at scale → content-addressed keys  
- Need async unpack/virus-scan → explicit artifact state machine + outbox  
- Multi-node storage → shared object store behind `ArtifactStorage`  
