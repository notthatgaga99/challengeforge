# Large Upload Architecture — Streaming, Partial Failure & Resumability

**Status:** Measured — **KEEP + temporary/finalize boundary**  
**ADR:** `docs/adr/0009-artifact-storage-consistency.md` (extended)  
**Harness:** `scripts/large_upload_architecture_experiment.py`  
**Results:** `docs/large-upload-architecture-results.json`

> **Streaming ≠ resumability.**  
> **Checksum ≠ deduplication.**

Default product limit remains **5 MiB** (`artifact_max_bytes`). Larger sizes in
the harness are for architecture evidence only.

---

## 1. Problem

As artifact size and upload concurrency grow, does ChallengeForge need:

- in-memory buffering,
- stream-to-temp + finalize,
- chunked / resumable uploads,
- or direct object-storage uploads?

Choose the smallest architecture that preserves the artifact invariant and
protects the interactive control plane.

---

## 2. Current architecture (before → after)

### Before

```text
HTTP multipart → await file.read()  # full body in RAM
              → storage.put(bytes)  # tmp sibling then replace
              → commit artifact_key
```

### After (this decision)

```text
HTTP multipart → stream chunks → .incoming/{uuid}  # disk staging + sha256
              → put_from_path → final key          # atomic publish
              → commit artifact_key
              → compensate / replace-GC / incoming cleanup
```

Invariant unchanged:

> Final `artifact_key` never refers to partially written bytes.  
> Product existence = durable key reference **and** complete blob.

---

## 3. Measurements

Laptop size sweep (see results JSON):

| Size | Buffered RSS Δ | Stream RSS Δ | Notes |
|---|---|---|---|
| 1 MiB | ~2 MiB | ~1 MiB | both fine |
| 10 MiB | **~10 MiB** | ~1 MiB | buffer tracks size |
| 25 MiB | **~25 MiB** | ~1 MiB | |
| 50 MiB | **~50 MiB** | ~1 MiB | |

Streaming throughput remained competitive (~70–80 MiB/s) vs buffered put after
allocation. Concurrency 1/5/10 × 256 KiB: all keys published, RSS stable.

Default **5 MiB** product cap remains; streaming is the safer default as limits
or concurrency rise. Numbers are **not** production SLOs.

---

## 4. Failure matrix

| Case | Bytes | DB | Recovery |
|---|---|---|---|
| Client disconnect mid-stream | staging aborted | none | no final key |
| Oversize mid-stream | staging deleted | none | ValidationFailed |
| Checksum mismatch | staging deleted | none | ValidationFailed |
| Finalize OK, commit fails | compensate delete final | rolled back | invariant holds |
| Commit OK | final present | referenced | exists |
| Abandoned `.incoming/` | cleanup after grace | none | reconciler / opportunistic cleanup |
| Replace attach | old key GC after commit | new key | safe |
| Retry whole upload | new staging → new key | last wins | no upload-ID required yet |

---

## 5. Design space

| Option | Memory | Failure | Complexity | Chosen? |
|---|---|---|---|---|
| A Buffer whole body | O(size) | simple | low | Was current API |
| **B Temp + finalize stream** | O(chunk) | strong partial isolation | low–med | **Yes** |
| C Chunked upload | O(chunk) | needs assembly protocol | med | No |
| D Resumable | O(chunk) | session state | high | Not justified |
| E Direct object storage | client/offload | provider semantics | high | Migration later |
| F Hybrid | — | — | high | Later |

---

## 6. Resumability assessment

Typical hackathon artifacts (source zips) and the **5 MiB** default make
**whole-upload retry** sufficient. Resumability is justified only when:

- max size grows into 100s of MiB–GiB,
- networks are unreliable enough that retries dominate,
- or concurrent large uploads contend badly with interactive traffic.

Not observed as a product requirement yet.

---

## 7. Checksum assessment

Optional whole-upload `X-Content-SHA256` verified while streaming.

| Protects | Does not |
|---|---|
| Accidental corruption / wrong body | Deduplication |
| Client/server disagreement on bytes | Malware / hostile content |
| Interrupted body producing wrong digest | CAS identity |

Per-chunk checksums and content-addressed keys deferred.

---

## 8. Idempotency / identity

No durable upload-ID. Each attempt stages a new ephemeral UUID; successful
finalize publishes a new artifact key; replace GC removes the previous key.
Retries may create orphans closed by compensate / reconcile — acceptable at
current scale.

---

## 9. Temporary vs final paths

```text
.incoming/{upload_uuid}   →  submissions/{challenge}/{participant}/{uuid}
```

Final key appears only after the complete file is published via
`put_from_path` (temp sibling + `os.replace`). Partial bytes never become the
referenced object.

---

## 10. Concurrent upload / interactive impact

Harness concurrency cells (256 KiB × 1/5/10) validate correctness under parallel
finalize. Interactive HTTP regression under large uploads was not shown to
require a separate upload plane at the default 5 MiB cap; revisit if max size
or concurrency rises.

---

## 11. Object-storage migration boundary

Keep domain on `ArtifactStorage`:

- `put` / `put_from_path` / `get` / `exists` / `delete` / `iter_keys` / `mtime` /
  `incoming_root`

Future `ObjectArtifactStore` can map `put_from_path` → multipart upload and
`incoming_root` → provider staging **without** leaking S3 APIs into submission
use cases. Do not build a mega-framework now.

---

## 12. Decision

### B — KEEP + temporary/finalize boundary

Stream uploads to staging, atomically publish complete blobs, keep blob-first
metadata + compensating delete + reconciliation. **Do not** add resumable or
chunked protocols or S3 yet.

---

## 13. Reconsideration triggers

- `artifact_max_bytes` raised past ~50–100 MiB with unreliable clients  
- Upload traffic degrades interactive p95 under measured load  
- Multi-node API needs shared blob backend  
- Need dedupe → CAS  
- Need virus-scan/unpack → ingestion state machine  

---

## What we prove / do not prove

**Prove:** full-body buffering is the prior memory risk; streaming+finalize
preserves no-partial-final invariant; optional SHA-256 works; abandoned staging
is reclaimable.

**Do not prove:** production capacity; that resumability is never needed; object
store performance.
