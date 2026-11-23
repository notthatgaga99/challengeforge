# ChallengeForge engineering curriculum record

Rolling log of fast-build curriculum milestones.  
**Built ≠ Understood ≠ Can design ≠ Can defend in an interview.**

---

## Semantic Retrieval Implementation (current)

| Field | Entry |
|---|---|
| Decision | **KEEP hybrid RRF as candidate default** (`cf-hybrid-rrf-1`); live default still `cf-lex-fts-1` |
| Concepts learned | Bi-encoder geometry; `embedding_version`; cosine on L2-normalized vectors; RRF fusion; lexical vs dense failure split |
| Alternatives understood | Dense-only · hybrid RRF · stub (CI) · curriculum concept encoder · optional MiniLM · dedicated vector DB (deferred) |
| Experiment | `scripts/semantic_retrieval_experiment.py` → `docs/semantic-retrieval-results.json` |
| Measured | Lexical paraphrase zero-hits=2; dense=0; hybrid Recall@5=1.0 / MRR=1.0 on judged queries (curriculum encoder) |
| Evidence proves | Semantic retrieval can recover paraphrase misses lexical fails; hybrid preserves exact/identifier wins |
| Does not prove | Production MiniLM/E5 quality; web-scale ANN; RAG answer quality |
| Can explain | Why hybrid beats either alone on this corpus; why stub ≠ semantic proof; why version embeddings |
| Still cannot explain confidently | Best RRF depth in production; when to add cross-encoder rerank |
| Evidence in repo | ADR 0013, design doc, results JSON, `challengeforge.retrieval.dense` / `.embeddings` |
| Reconsideration triggers | Scale · MiniLM comparison · identifier regressions · ranking-only failures · tenancy |

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
