# Semantic Retrieval: Standards → Choice → Design

**Status:** Measured — **KEEP hybrid RRF as candidate default** (`cf-hybrid-rrf-1`)  
Live retrieve default remains `cf-lex-fts-1` until product flip.  
**ADR:** `docs/adr/0013-semantic-retrieval.md`  
**Results:** `docs/semantic-retrieval-results.json`  
**Harness:** `scripts/semantic_retrieval_experiment.py`  
**Lexical control:** `docs/lexical-retrieval.md` / ADR 0012  
**Corpus (reuse):** `docs/lexical-retrieval-corpus.json`

> Curriculum concept embeddings demonstrate semantic geometry offline.  
> Optional MiniLM when `sentence-transformers` is installed. Stub ≠ semantic proof.

LLM answer generation, cross-encoder product reranking, Elasticsearch, and
dedicated vector DBs as required infra are **out of scope**.

---

## 1. Problem

Lexical retrieval (`cf-lex-fts-1`) is measurable and strong when query terms
appear in chunks. The checked-in corpus also contains **semantic paraphrase**
queries that systematically miss relevant chunks that use different wording.

Industry practice for that failure mode is dense (embedding) retrieval, usually
combined with lexical search. ChallengeForge must choose an approach that fits:

- modular monolith + PostgreSQL system of record
- versioned parser/chunker already in place
- offline/CI-friendly curriculum defaults
- apples-to-apples eval against the lexical baseline

```text
industry options
      ↓
ChallengeForge constraints
      ↓
pick
      ↓
contract (embedding_version, storage, retrieve versions)
      ↓
eval plan (same corpus, Recall@K / MRR)
      ↓
implement later → measure → KEEP or reconsider
```

---

## 2. Industry standards (research)

### 2.1 Dense bi-encoder retrieval

**What it is:** Encode query and chunk independently into vectors; rank by
cosine similarity or inner product (exact KNN or ANN).

| | |
|---|---|
| **Solves** | Paraphrase / synonymy when the model geometry aligns concepts |
| **Fails** | Exact identifiers, rare tokens, precise phrases (often) |
| **Cost** | Embed corpus + query; storage for vectors; model ops |
| **Coupling** | Chunk size, embedding model, and similarity metric interact |

This is the industry default meaning of “semantic search.”

### 2.2 Cross-encoder reranking

**What it is:** Score `(query, chunk)` pairs jointly after a wide candidate set
(lexical and/or dense).

| Pros | Cons |
|---|---|
| Often best pairwise relevance | Expensive; needs candidates first |
| Improves precision in top-k | Not a standalone retrieval system |

**ChallengeForge:** defer until dense + lexical candidates exist and eval shows
ranking (not recall) is the bottleneck.

### 2.3 Dedicated vector databases

Pinecone, Qdrant, Weaviate, Milvus, etc.

| Pros | Cons |
|---|---|
| Scale, ANN ops, filter DSLs | New system; dual durability/tenancy |
| Managed offerings | Laptop corpus does not justify ops cost |

**ChallengeForge:** reject as *required* infra until Postgres+pgvector fails
measured latency/recall at real scale.

### 2.4 pgvector (vectors in PostgreSQL)

Store embeddings beside `document_chunks`; query with `<->` / cosine ops;
optional IVFFlat/HNSW indexes.

| Pros | Cons |
|---|---|
| One DB, joins to provenance/filters | Extension dependency |
| Fits prior “KEEP Postgres” ADRs | ANN ops less specialized than dedicated DBs at huge scale |
| Transactional backfill with jobs | Need fallback story for environments without the extension |

### 2.5 Hybrid lexical + dense

**Reciprocal Rank Fusion (RRF)** (common, parameter-light):

\[
\mathrm{RRF}(d) = \sum_{r \in R} \frac{1}{k + \mathrm{rank}_r(d)}
\]

or weighted score fusion (more knobs).

| Pros | Cons |
|---|---|
| Lexical wins identifiers; dense wins paraphrase | Two pipelines to operate |
| Industry production default | Fusion constants need eval, not vibes |

### 2.6 Embedding providers

| Provider class | Pros | Cons |
|---|---|---|
| Hosted API (OpenAI / Cohere / Voyage) | Quality, easy ops | Cost, network, CI non-determinism, data egress |
| Local models (MiniLM, E5, BGE) | Offline, controllable | Dep weight, GPU/CPU cost, version pins |
| Deterministic stub (hash→vector) | CI determinism, pipeline teaching | **Not** semantic proof |

---

## 3. ChallengeForge selection criteria

1. Postgres remains system of record (ADRs 0002/0004/0010/0012).  
2. Same eval corpus + Recall@K / MRR as lexical.  
3. Offline / CI-friendly default (no paid API required for core path).  
4. Versioning: `parser_version` / `chunker_version` / **`embedding_version`**.  
5. Search only succeeded ingestion jobs.  
6. No “embeddings are better” without beating `cf-lex-fts-1`.  
7. No second datastore without measured need.

---

## 4. Decision (ChallengeForge pick)

### KEEP: Dense bi-encoder + pgvector-in-Postgres + Hybrid RRF

Measured later against lexical; hybrid is the *candidate* product default **if**
eval shows paraphrase gains without destroying identifier queries.

| Layer | Choice | Why for this case |
|---|---|---|
| Storage | Postgres + **pgvector** | Same durability/tenancy/joins as chunks |
| API shape | Extend existing `retrieve(..., retrieval_version=...)` | Stable contract from ADR 0012 |
| Encoder | `EmbeddingProvider` + `embedding_version` | Swap models without rewriting search |
| Experiments | Local small sentence model | Real semantic signal offline |
| Unit tests | Deterministic stub encoder | CI without model downloads |
| Hosted APIs | Not default | Cost + non-determinism |
| Dedicated vector DB | Reject for now | No scale evidence |
| Cross-encoder | Deferred | Needs candidates + ranking-failure evidence |
| Fusion | **RRF** (`cf-hybrid-rrf-1`) | Simple, standard, few knobs |

```text
query
  ├─► cf-lex-fts-1 (existing)
  └─► embed(query) → cf-dense-1 (pgvector KNN)
              ↓
        cf-hybrid-rrf-1
              ↓
         top-k + provenance
              ↓
    same corpus metrics vs lexical
```

---

## 5. Retrieval contract sketch

### 5.1 Versions

| `retrieval_version` | Behavior |
|---|---|
| `cf-lex-fts-1` (existing) | Lexical control |
| `cf-dense-1` | Dense-only KNN over one `embedding_version` |
| `cf-hybrid-rrf-1` | RRF of lexical top-N + dense top-N |

Tie-break after fusion: fused score DESC, then `chunk_id` ASC (deterministic).

### 5.2 Embedding identity

```text
embedding_version = "<provider>:<model>:<dim>:<metric>"
e.g. "local:all-MiniLM-L6-v2:384:cosine"
     "stub:cf-hash-v1:64:cosine"   # tests only
```

Rules:

- One retrieve call uses **exactly one** `embedding_version`.  
- Changing model/dim/metric ⇒ new `embedding_version` ⇒ new rows (no silent mix).  
- Re-embed / backfill when version changes; old vectors retained until GC policy says otherwise.

### 5.3 Storage sketch (implementation later)

Prefer a **side table** (clear versioning, no wide row on every chunk mutate):

```text
chunk_embeddings
  chunk_id          UUID FK → document_chunks(id) ON DELETE CASCADE
  embedding_version TEXT NOT NULL
  embedding         vector(D)   -- pgvector; D fixed per embedding_version
  content_sha256    TEXT NOT NULL  -- detect stale vs chunk content
  created_at        timestamptz
  PRIMARY KEY (chunk_id, embedding_version)
```

Index (per version / later): HNSW or IVFFlat on `embedding` when corpus size
warrants; exact scan acceptable for the current eval corpus.

Join path must enforce:

```text
document_chunks ⋈ ingestion_jobs(status = 'succeeded')
```

### 5.4 Provider interface (sketch)

```text
EmbeddingProvider
  embedding_version: str
  dimensions: int
  embed_documents(texts: list[str]) -> list[vector]
  embed_query(text: str) -> vector
```

- **Stub:** deterministic; for unit tests / pipeline wiring only.  
- **Local model:** experiment harness + optional worker backfill.  
- **API provider:** optional later behind the same interface.

### 5.5 Ingestion coupling

Embeddings are **downstream of durable chunks**:

```text
ingestion succeeded + document_chunks committed
        ↓
  embed job / inline backfill (follow-up design)
        ↓
  chunk_embeddings rows for embedding_version
```

Partial embedding sets must not be treated as complete for that version
(mirror chunk-set durability thinking: version + expected coverage).

---

## 6. Evaluation results (laptop, curriculum:concept-v1)

Reuse [`docs/lexical-retrieval-corpus.json`](docs/lexical-retrieval-corpus.json).  
Full machine-readable output: `docs/semantic-retrieval-results.json`.

| Version | MRR | Recall@5 | Paraphrase zero-hits |
|---|---|---|---|
| `cf-lex-fts-1` | 0.75 | 0.80 | **2** |
| `cf-dense-1` | 0.86 | 0.90 | **0** |
| `cf-hybrid-rrf-1` | **1.00** | **1.00** | **0** |

**Decision:** hybrid RRF is the **candidate** product default. Live default stays
lexical until an explicit flip. Curriculum encoder demonstrates the *effect*;
MiniLM bakeoff measured in ADR 0014 / `docs/minilm-bakeoff-results.json`.

---

## 7. Failure hypotheses (what we expect)

| Hypothesis | Predicted winner |
|---|---|
| Paraphrase queries | Dense / hybrid > lexical |
| `recover_stale_running`, `FOR UPDATE SKIP LOCKED` | Lexical / simple ≥ dense |
| Stop-word / empty intent | All weak; do not overclaim |
| Lexically similar noise docs | Hybrid may need tuning or later rerank |

---

## 8. What this design does NOT claim

- Embeddings are better (unmeasured).  
- pgvector ≡ production ANN at web scale.  
- Stub embeddings prove semantic quality.  
- Hybrid RRF constants are optimal.  
- RAG answers or citation faithfulness (later milestones).

---

## 9. Reconsideration triggers

- pgvector latency/recall inadequate at real corpus size → revisit dedicated vector DB  
- Local model too weak on paraphrase → try stronger local / optional API under same interface  
- Identifier regressions under hybrid → adjust RRF depth / lexical weight / query routing  
- Ranking failures with good recall → cross-encoder rerank experiment  
- Multi-tenant authz becomes product-critical → harden filters before expanding retrieval

---

## 10. Implementation gate

**This milestone stops at design + ADR + curriculum.**

Do **not** add embedding dependencies, migrations, or retrieve versions to
production code until a follow-up milestone implements and **measures** against
the lexical baseline.
