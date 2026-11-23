# ChallengeForge engineering curriculum record

Rolling log of fast-build curriculum milestones.  
**Built ≠ Understood ≠ Can design ≠ Can defend in an interview.**

---

## Transactional Outbox (current)

| Field | Entry |
|---|---|
| Decision | **KEEP Postgres `outbox_events` + poll relay + pluggable sink** |
| Concepts learned | Dual-write problem; same-TX enqueue; at-least-once; sink idempotency; SKIP LOCKED claim |
| Alternatives understood | Sync notify after commit · external queue dual-write · CDC · outbox |
| Evidence in repo | ADR 0017, `docs/transactional-outbox-design.md`, `challengeforge.outbox`, tests |
| Events | `evaluation.succeeded` · `hackathon.closed` |

Suggested interview prompts:

1. What is the dual-write problem between a DB and a webhook?  
2. How does a transactional outbox fix it?  
3. Why is delivery at-least-once, and what must sinks do?  
4. Why prefer SKIP LOCKED over a single locked poller row?

---

## Leaderboard Consistency

| Field | Entry |
|---|---|
| Decision | **KEEP Postgres `leaderboard_entries` snapshot** upserted in same TX as eval succeed |
| Concepts learned | Snapshot vs live query; publish gate; deterministic tie-break; `RANK()` |
| Alternatives understood | Live ORDER BY evaluations · Redis ZSET · TX snapshot |
| Evidence in repo | ADR 0016, `docs/leaderboard-consistency-design.md`, migration `0015` |
| Visibility | Organizer always; participants after hackathon `closed` |

---

## Search Tenancy / Authz

| Field | Entry |
|---|---|
| Decision | **KEEP deny-by-default effective SQL scope** via `resolve_retrieval_scope` |
| Concepts learned | Tenancy in retrieval; client filters cannot widen; join-based enforcement |
| Alternatives understood | App post-filter · Postgres RLS · deny-by-default SQL filters |
| Evidence in repo | ADR 0015, `docs/search-tenancy-design.md`, `retrieval/authz.py`, search API |
| Policy | Organizer = owned hackathon; participant = own submissions only |

---

## MiniLM Bakeoff

| Field | Entry |
|---|---|
| Decision | **KEEP MiniLM as preferred real encoder**; curriculum stays offline/CI demo; **live default still lexical** |
| Concepts learned | Educational encoder ≠ production claim; same-corpus bakeoffs; optional heavy deps (`[embeddings]`) |
| Alternatives understood | Curriculum concept-v1 · MiniLM-L6-v2 · stub hash · (deferred E5/BGE, cross-encoder) |
| Experiment | `scripts/minilm_bakeoff_experiment.py` → `docs/minilm-bakeoff-results.json` |
| Measured | Lex MRR 0.75 / para-zero 2; curriculum hybrid MRR 1.0; MiniLM hybrid MRR 0.9; both dense/hybrid para-zero 0 |
| Evidence proves | Real MiniLM closes paraphrase gap vs lexical; curriculum can outperform MiniLM on a jargon-tuned toy corpus |
| Does not prove | MiniLM ranking quality at scale; that curriculum generalizes; RAG quality |
| Can explain | Why curriculum beating MiniLM here is not a production claim; why live default stays lexical |
| Still cannot explain confidently | When to promote hybrid live; cross-encoder value; E5/BGE vs MiniLM |
| Evidence in repo | ADR 0014, bakeoff results JSON |
| Reconsideration triggers | Larger corpus · promote-hybrid · identifier regressions · stronger local models |

Suggested interview prompts:

1. Why might a hand-tuned toy encoder beat MiniLM on a 10-query set?  
2. What does a fair encoder bakeoff hold constant?  
3. Why keep heavy embedding deps optional?  
4. When would you flip the live default to hybrid?

---

## Semantic Retrieval Implementation

| Field | Entry |
|---|---|
| Decision | **KEEP hybrid RRF as candidate default** (`cf-hybrid-rrf-1`); live default still `cf-lex-fts-1` |
| Concepts learned | Bi-encoder geometry; `embedding_version`; cosine on L2-normalized vectors; RRF fusion; lexical vs dense failure split |
| Alternatives understood | Dense-only · hybrid RRF · stub (CI) · curriculum concept encoder · optional MiniLM · dedicated vector DB (deferred) |
| Experiment | `scripts/semantic_retrieval_experiment.py` → `docs/semantic-retrieval-results.json` |
| Measured | Lexical paraphrase zero-hits=2; dense=0; hybrid Recall@5=1.0 / MRR=1.0 on judged queries (curriculum encoder) |
| Evidence proves | Semantic retrieval can recover paraphrase misses lexical fails; hybrid preserves exact/identifier wins |
| Does not prove | Production MiniLM/E5 quality at scale; web-scale ANN; RAG answer quality (partially addressed by ADR 0014 bakeoff) |
| Can explain | Why hybrid beats either alone on this corpus; why stub ≠ semantic proof; why version embeddings |
| Still cannot explain confidently | Best RRF depth in production; when to add cross-encoder rerank |
| Evidence in repo | ADR 0013, design doc, results JSON, `challengeforge.retrieval.dense` / `.embeddings` |
| Reconsideration triggers | Scale · MiniLM comparison (done → ADR 0014) · identifier regressions · ranking-only failures · tenancy |

Suggested interview prompts (design-level; not marked mastered):

1. Bi-encoder vs cross-encoder — when do you use each?  
2. Why keep lexical search after adding embeddings?  
3. What is Reciprocal Rank Fusion and why prefer it over weighted score soup early?  
4. What does `embedding_version` protect you from?  
5. pgvector vs Pinecone — how do you decide?  
6. Why is a hash-stub encoder insufficient as “semantic search”?  
7. How do you compare dense vs lexical fairly?  
8. What failure mode motivated this milestone from the lexical baseline?

---

## Lexical Retrieval Baseline

| Field | Entry |
|---|---|
| Decision | KEEP PostgreSQL lexical FTS baseline (`ts_rank_cd`, not BM25) |
| Evidence in repo | ADR 0012, `docs/lexical-retrieval.md`, results JSON |
| Open hypothesis | Paraphrase / semantic_gap → addressed by semantic design (ADR 0013) |

Suggested interview prompts (lexical):

1. Why isn't exact substring matching enough?  
2. What does an inverted index buy you?  
3. What is TF-IDF?  
4. What problem does BM25 solve?  
5. How does PostgreSQL full-text search tokenize text?  
6. What is `tsvector` / `tsquery`?  
7. Why does lexical retrieval miss paraphrases?  
8. Recall@K vs MRR?  
9. Candidate generation vs ranking?  
10. When does lexical remain valuable after vectors?

---

## Chunking & Representation

| Field | Entry |
|---|---|
| Decision | KEEP hybrid structure-aware chunking + char budget; Postgres `document_chunks` |
| Evidence in repo | ADR 0011, `docs/chunking-and-representation.md` |

---

## Prior milestones (pointers)

| Milestone | Decision | Doc |
|---|---|---|
| Ingestion pipeline reliability | KEEP Postgres durable jobs | `docs/ingestion-pipeline-reliability.md` / ADR 0010 |
| Large uploads | KEEP stream + finalize | `docs/large-upload-architecture.md` |
| Artifact consistency | BLOB-FIRST + compensate + reconcile | `docs/artifact-storage-consistency.md` / ADR 0009 |
| Isolation boundary | KEEP subprocess for trusted synthetic only | `docs/execution-isolation-boundary.md` / ADR 0008 |
