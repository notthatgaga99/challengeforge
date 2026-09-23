# ChallengeForge engineering curriculum record

Rolling log of fast-build curriculum milestones.  
**Built ≠ Understood ≠ Can design ≠ Can defend in an interview.**

---

## Lexical Retrieval Baseline (current)

| Field | Entry |
|---|---|
| Decision | KEEP PostgreSQL lexical FTS baseline (`ts_rank_cd`, not BM25); versions fts / simple / struct |
| Concepts learned | Candidate generation vs ranking; Recall@K vs MRR; semantic gap vs ranking failure; `tsvector`/`tsquery` |
| Alternatives understood | Exact match · PG FTS · TF-IDF · BM25 · structure-aware lexical · (deferred) embeddings |
| Experiment | `scripts/lexical_retrieval_experiment.py` + checked-in corpus |
| Measured results | See `docs/lexical-retrieval-results.json` |
| Failure modes | Paraphrase/semantic gap is the designed lexical failure; noise from lexically similar docs |
| Evidence proves | Lexical retrieval is measurable and useful when terms overlap; PG remains fast at laptop corpus sizes |
| Does not prove | Embeddings are better; production search SLOs; BM25 equivalence |
| Can explain | Why start lexical before vectors; why high Recall@10 can still feel bad; why paraphrase queries matter |
| Still cannot explain confidently | Optimal hybrid lexical+vector fusion weights; production multi-tenant search isolation design |
| Interview questions | See `docs/lexical-retrieval.md` curriculum section / milestone prompt list |
| Reconsideration triggers | Scale · BM25 need · tenancy · paraphrase saturation |

Suggested interview prompts (not marked mastered):

1. Why isn't exact substring matching enough?  
2. What does an inverted index buy you?  
3. What is TF-IDF?  
4. What problem does BM25 solve?  
5. How does PostgreSQL full-text search tokenize text?  
6. What is `tsvector`?  
7. What is `tsquery`?  
8. Why does lexical retrieval miss paraphrases?  
9. What does Recall@K measure?  
10. Why can high Recall@10 coexist with poor UX?  
11. What is MRR?  
12. Candidate generation vs ranking?  
13. Why lexical before embeddings?  
14. When does lexical remain valuable after vectors?

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
