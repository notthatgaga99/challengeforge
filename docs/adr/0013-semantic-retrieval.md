# ADR 0013: Semantic retrieval design (dense + pgvector + hybrid RRF)

## Status

Accepted **and measured**: **Dense bi-encoder + `chunk_embeddings` (float[]) + Hybrid RRF**.

Live API default remains lexical `cf-lex-fts-1`. Hybrid `cf-hybrid-rrf-1` is the
**candidate** product default after measured paraphrase lift (see results JSON).

Storage note: portable `double precision[]` + in-process cosine (L2-normalized);
pgvector-compatible contract without requiring the extension in CI.

## Context

Lexical FTS is durable and measured, but paraphrase queries in
`docs/lexical-retrieval-corpus.json` expose a **semantic gap**. Industry
response is dense retrieval, usually hybridized with lexical search. ChallengeForge
must choose storage and providers that fit the modular monolith and Postgres-first
curriculum without jumping to a vector product unmeasured.

## Decision

1. **Adopt** dense bi-encoder retrieval as the next retrieval experiment.  
2. **Store** embeddings in PostgreSQL via **pgvector**, keyed by
   `(chunk_id, embedding_version)` (side table preferred).  
3. **Expose** retrieval versions `cf-dense-1` and `cf-hybrid-rrf-1` beside
   existing `cf-lex-*` versions through the same `retrieve` contract.  
4. **Fusion:** Reciprocal Rank Fusion for hybrid.  
5. **Providers:** `EmbeddingProvider` interface; local model for experiments;
   deterministic stub for unit tests; hosted APIs **not** the default.  
6. **Eval:** Same corpus and Recall@K / MRR as lexical before any product-default
   change.  
7. **Defer:** dedicated vector DB, cross-encoder product rerank, LLM/RAG answers.  
8. **Refuse** to claim “embeddings are better” without beating `cf-lex-fts-1`.

## Alternatives

| Option | Why not (now) |
|---|---|
| Embeddings-only product default | Identifier/exact regressions likely; no lexical control |
| Pinecone / Qdrant / Weaviate required | Extra system; no scale evidence |
| Hosted embedding API as default | Cost, egress, CI non-determinism |
| Cross-encoder first | Needs candidate generation; ranking-second problem |
| Skip dense; only improve BM25 | Does not address paraphrase hypothesis |

## Evidence (design)

- `docs/semantic-retrieval-design.md`  
- Lexical failure taxonomy: `docs/lexical-retrieval.md` / results JSON  
- Prior KEEP Postgres ADRs (0004, 0010, 0012)

## Trade-offs

**+** Industry-standard path; Postgres-aligned; measurable; versioned  
**−** Extension dependency; model ops; hybrid complexity; unproven until measured

## Reconsideration

pgvector inadequate at scale · hybrid hurts identifiers ·
ranking-only failures → rerank · tenancy filters become mandatory ·
promote hybrid to live default (product gate)

MiniLM vs curriculum bakeoff: **ADR 0014**.

## Related

ADR 0012 (lexical baseline), ADR 0011 (chunking), ADR 0010 (ingestion),
ADR 0014 (MiniLM bakeoff).
