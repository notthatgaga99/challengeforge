# Chunking & Representation

**Status:** Measured — **KEEP hybrid structure-aware chunking (character budget)**  
**ADR:** `docs/adr/0011-chunking-and-representation.md`  
**Harness:** `scripts/chunking_representation_experiment.py`  
**Results:** `docs/chunking-and-representation-results.json`  
**Code:** `challengeforge.ingestion.canonical`, `.chunking`, Postgres `document_chunks`  
**Tests:** `tests/test_chunking_representation.py`

Embeddings / vector DBs / RAG / reranking are **out of scope**.

---

## 1. Problem

A committed, normalized artifact is still too coarse for later retrieval and
evidence. Blind fixed splitting destroys Markdown/code coherence and loses
source location. ChallengeForge needs a **retrieval-oriented chunk contract**
with durable identity, versioning, and provenance — before any embedding model
exists.

Naive `" ".join(text.split())` normalization (ingestion v1) also destroyed
structure. This milestone upgrades the canonical form so offsets are meaningful.

## 2. Design space

| Strategy | Coherence | Complexity | Determinism | Provenance | Model coupling | Code/prose |
|---|---|---|---|---|---|---|
| **A Fixed characters** | Weak (mid-paragraph / mid-fence) | Low | High | Easy offsets, poor semantics | Low | Bad on both |
| **B Token budget** | Better for LLMs | Med | Med (tokenizer) | Harder | **High** | Needs tokenizer choice |
| **C Structure-only** | Strong | Med | High | Strong | Low | Unbounded huge blocks |
| **D Recursive/hierarchical** | Strong | Higher | High | Strong | Low | Good, more moving parts |
| **E Hybrid (structure + budget)** | Strong when possible | Med | High | Strong if span-exact | Low | Hard-splits pathological units |

Token-oriented sizing is premature: no embedding/chat model is selected yet.
Claiming “512 tokens” without a tokenizer is theater.

## 3. Chosen representation

### KEEP: Hybrid structure-aware packing + Unicode character budget

Pipeline:

```text
artifact bytes
  → cf-parse-2 canonical text (structure-preserving)
  → unitize (headings / fences / paragraphs / code blocks)
  → hard-split oversized units
  → pack contiguous spans under max_chars
  → document_chunks + canonical.txt + result.json
```

Defaults:

- `PARSER_VERSION = cf-parse-2`
- `CHUNKER_VERSION = cf-chunk-hybrid-1`
- `ingestion_chunk_max_chars = 1200` (Unicode **code points**, not bytes/tokens)
- **overlap = 0** (exact spans; no duplicated regions)

Why characters: deterministic, model-agnostic, cheap to measure, honest about
not being a token budget. Revisit when an embedding model is chosen.

## 4. Provenance model

Offsets are against **canonical text**, not raw upload bytes.

| Field | Meaning |
|---|---|
| `char_start` / `char_end` | Half-open span in canonical text |
| `line_start` / `line_end` | 1-based lines covering that span |
| `heading_path` | Markdown heading stack at unitization |
| `block_type` | heading / paragraph / code_fence / code / list / mixed / hard_split |

**Invariant:** `canonical[char_start:char_end] == chunk.content`

Normalization changes (CRLF→LF, trailing spaces) mean raw-byte offsets are
**not** interchangeable with canonical offsets. Cite via canonical + artifact_key.

## 5. Identity / versioning

| Concept | Rule |
|---|---|
| Row PK | Random UUID |
| Set identity | `ingestion_job_id` (one complete set per successful job) |
| Order | `ordinal` 0..n-1, unique per job |
| Versions | `parser_version` + `chunker_version` stamped on every row |
| Content fingerprint | `content_sha256` |
| Same artifact re-ingested | New job → new chunk set (history retained via jobs) |
| Same job retried | `replace_for_job` deletes+inserts (idempotent) |
| Chunker/parser change | New version strings; old rows untouched |

Same bytes ≠ same chunks if parser/chunker/config differ. Version fields prevent
silent masquerading.

## 6. Durability

A chunk set is **complete** only when:

1. `ingestion_jobs.status = succeeded`
2. `result.json` exists with `stage=CHUNKED` and matching `chunk_count`
3. `document_chunks` rows for that job were written in the **same DB transaction**
   as the succeed transition

Consumers must ignore chunks for non-succeeded jobs (partial persist / crash).
Stale recovery deletes partial chunk rows + blob outputs before requeue.

Empty documents: zero chunks + succeeded is valid (no empty chunk rows).

## 7. Experiments

See results JSON. Hybrid vs fixed on Markdown retains `heading_path`; oversized
and long-line corpora stay bounded; deterministic repeats match; memory scale
on 1e5 chars remains laptop-friendly.

## 8. Failure modes

| Failure | Behavior |
|---|---|
| Chunker crash | Job stays `running` → stale recovery cleans partials |
| Empty / whitespace-only | 0 chunks, success |
| Oversized unit | Hard-split at budget (newline/space preferred) |
| Invalid UTF-8 | Deterministic ingest failure (existing) |
| Duplicate delivery | replace_for_job + conditional succeed |
| Version change | New stamps; no silent overwrite of old semantics |
| DB fail mid-persist | Transaction abort; not succeeded |

## 9. What this does NOT solve

Chunking alone does **not** establish retrieval quality, semantic similarity,
embedding quality, ranking, citation correctness, or RAG answer quality.

## 10. Reconsideration triggers

- Retrieval experiments show systematic boundary failures  
- Selected embedding model needs different size/overlap  
- Provenance needs byte-accurate raw mapping or AST symbols  
- Incremental re-ingest cost too high  
- Postgres chunk storage insufficient at corpus scale  
- Code retrieval requires symbol-level structure  

---

## Engineering progress record

| | |
|---|---|
| **Milestone** | Chunking & Representation |
| **Decision** | KEEP hybrid + char budget; Postgres `document_chunks` |
| **Concepts** | Canonical vs raw offsets; at-least-once + replace; versioned representations |
| **Alternatives** | Fixed / token / structure-only / recursive / hybrid |
| **Experiment** | `scripts/chunking_representation_experiment.py` |
| **What failed** | First pack joined units with `\n` and broke span equality — fixed to contiguous canonical spans |
| **Proves** | Deterministic bounded chunks with exact provenance at laptop scale |
| **Does not prove** | Retrieval/embedding/RAG quality |
| **Can explain** | Why token budgets are premature; why overlap was deferred; durability of chunk sets |
| **Cannot yet explain confidently** | Optimal chunk size for a specific embedding model; symbol-level code chunking trade-offs in production |
| **Interview prep** | Chunk identity under re-ingest; provenance vs normalization; exactly-once effects via idempotent replace |
| **Built ≠ Understood ≠ Defendable** | Treat retrieval claims as unproven until measured |
