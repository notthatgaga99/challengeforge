# ADR 0012: Lexical retrieval baseline

## Status

Accepted: **PostgreSQL full-text search baseline** over `document_chunks`
(`tsvector` + GIN + `ts_rank_cd`). Default version `cf-lex-fts-1`.

## Context

Chunks are durable and provenance-rich, but ChallengeForge had no retrieval
plane and no measured quality baseline. Embedding/vector systems would be
unjustified fashion without a lexical control experiment.

## Decision

1. Add generated `search_tsv` / `search_tsv_simple` columns + GIN indexes.  
2. Expose `retrieve(query, top_k, filters, retrieval_version)`.  
3. Search only chunks from `ingestion_jobs.status = 'succeeded'`.  
4. Evaluate with a checked-in corpus + Recall@K / Precision@K / MRR.  
5. Record paraphrase failures as the hypothesis for a later semantic milestone.  
6. Do **not** claim PG ranking ≡ BM25. Do **not** add vector DBs/embeddings yet.

## Alternatives

| Option | Why not now |
|---|---|
| Exact `ILIKE` only | No inverted index; poor multi-term ranking |
| App-side BM25 | More code; PG FTS sufficient for baseline |
| Elasticsearch | Extra ops system without measured need |
| Embeddings first | No lexical control; unmeasured jump |

## Evidence

- `docs/lexical-retrieval.md`  
- `docs/lexical-retrieval-corpus.json`  
- `scripts/lexical_retrieval_experiment.py`  
- `tests/test_lexical_retrieval.py`  
- Migration `0013_lexical_retrieval`

## Trade-offs

**+** Native Postgres, explainable, fast at laptop scale, measurable  
**−** Semantic gaps; english stemmer vs identifiers; not BM25

## Reconsideration

Latency/scale · true BM25 need · tenancy filters · paraphrase saturation → vectors

## Related

ADR 0011 (chunking), ADR 0010 (ingestion).
