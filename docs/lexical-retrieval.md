# Lexical Retrieval Baseline & Evaluation

**Status:** Measured — **KEEP PostgreSQL lexical FTS baseline** (`ts_rank_cd`, not BM25)  
**ADR:** `docs/adr/0012-lexical-retrieval.md`  
**Corpus:** `docs/lexical-retrieval-corpus.json`  
**Harness:** `scripts/lexical_retrieval_experiment.py`  
**Results:** `docs/lexical-retrieval-results.json`  
**Code:** `challengeforge.retrieval`  
**Tests:** `tests/test_lexical_retrieval.py`

Embeddings / vector DBs / LLM / RAG answer generation are **out of scope**.

---

## 1. Retrieval problem

```text
query
  → query normalization
  → candidate generation   (which chunks could match?)
  → scoring
  → ranking
  → top-k
  → retrieved chunks (+ provenance)
```

**Candidate generation ≠ ranking ≠ answer generation.**  
This milestone stops at ranked chunks with measured quality. No synthesis.

## 2. Design space

| Approach | Matching | Paraphrase | Identifiers | Cost | Explainability | PG fit |
|---|---|---|---|---|---|---|
| Exact substring | Brittle | Fail | Good if literal | Seq scan risk | High | `ILIKE` |
| PG full-text (`tsvector`) | Stemmed tokens | Fail | Mixed | GIN index | Medium | Native |
| Classical TF-IDF | Term weights | Fail | Needs tokens | App-side | High | DIY |
| BM25 | Doc-length aware | Fail | Needs tokens | App-side / ext | High | Not native `ts_rank` |
| Structure-aware lexical | Lexical + meta | Partial | Boostable | Low extra | High | SQL boosts |

**Important:** PostgreSQL `ts_rank` / `ts_rank_cd` is **not** verified here as mathematical BM25. ChallengeForge treats PG FTS as a **practical lexical baseline**, not a BM25 clone.

## 3. PostgreSQL design

Migration `0013_lexical_retrieval`:

- `search_tsv` — `english` weighted (`heading_path` A, `content` B), **GIN**
- `search_tsv_simple` — `simple` dictionary on content (less stemming), **GIN**

Only chunks whose `ingestion_jobs.status = 'succeeded'` are searchable.

Versions:

| Version | Mechanism |
|---|---|
| `cf-lex-fts-1` | `websearch_to_tsquery('english')` + `ts_rank_cd(search_tsv)` |
| `cf-lex-fts-simple-1` | `plainto_tsquery('simple')` + `search_tsv_simple` |
| `cf-lex-fts-struct-1` | english FTS + bounded ILIKE boosts (heading / exact / code block) |

Tie-break: `score DESC, chunk_id ASC`.

## 4. Query semantics

| Query type | Behavior notes |
|---|---|
| Prose | english stemming helps variants (`retries` / `retry`) |
| Exact phrase | `websearch_to_tsquery` phrase handling; still not string equality |
| Code identifiers | `simple` config preserves underscores better than english stemmer |
| Rare terms | Strong when the token appears (`idempotency`) |
| Stop-word heavy | Often empty or noisy candidates |
| Unicode | Depends on PG text search config; must not error |
| Paraphrase | **Expected lexical failure** (semantic gap) |

Normalization is light (`split`/`join` whitespace only) — identifiers are not lowercased away.

## 5. Structural signals

`cf-lex-fts-struct-1` adds small, explicit boosts:

- +0.15 heading_path ILIKE query  
- +0.10 content ILIKE query  
- +0.05 code/code_fence ILIKE query  

Not a large hand-tuned ranker. Versioned and measured against pure FTS.

## 6. Evaluation corpus

Checked-in JSON with documents + queries + relevance selectors (`contains` within doc).  
Includes exact, identifier, rare term, multi-word, unicode, noise, no-result, and **semantic paraphrase** cases designed to fail lexically.

## 7. Metrics

For query \(q\) with relevant set \(R\) and ranked list \(L\):

\[
\mathrm{Recall}@K = \frac{|\{d \in R : d \in L[1..K]\}|}{|R|}
\]

\[
\mathrm{Precision}@K = \frac{|\{d \in L[1..K] : d \in R\}|}{K}
\]

\[
\mathrm{MRR} = \frac{1}{|Q|}\sum_{q\in Q} \frac{1}{\mathrm{rank}_q}
\]

where \(\mathrm{rank}_q\) is the rank of the first relevant hit (0 contribution if none).  
Queries with empty \(R\) are tracked separately (zero-hit rate), not folded into Recall.

## 8. Experiment results

From `docs/lexical-retrieval-results.json` (laptop; not production SLOs):

### Quality (`cf-lex-fts-1`, judged queries)

| Metric | Value |
|---|---|
| Recall@1 | 0.65 |
| Recall@3 / @5 / @10 | 0.80 |
| MRR | 0.75 |
| Zero-hit rate | 0.20 |
| Failure category | `semantic_gap` ×2 (designed paraphrase queries) |

Exact / rare-term / phrase queries hit well. Paraphrase queries miss — that is
the intentional lexical ceiling.

### Latency (synthetic corpus, FTS)

| ~Chunks | p50 (ms) | p95 (ms) |
|---|---|---|
| 100 | ~3.5 | ~5 |
| 1,000 | ~27 | ~31 |
| 10,000 | ~200 | ~336 |
| 100,000 | ~8.2s | ~10.4s |

At ~100k matching-heavy queries, laptop latency degrades — a reconsideration
signal for index tuning / query selectivity / eventual hybrid search, **not**
an automatic jump to a vector DB.

## 9. Failure taxonomy

| Category | Meaning |
|---|---|
| semantic_gap | Same idea, different words (paraphrase) |
| vocabulary_mismatch | Expected terms absent |
| ranking_failure | Relevant present but ranked too low / crowded by noise |
| tokenization_failure | Identifier/syntax mishandled |
| structural_failure | Heading/context ignored |
| query_interpretation_failure | Query parser misread input |
| noise | Lexically similar irrelevant chunks |

## 10. What lexical retrieval solves

Fast, explainable, indexable matching when query terms appear in chunks.  
A measurable baseline before any embedding spend.

## 11. What lexical retrieval cannot solve

Semantic paraphrase, synonymy without shared tokens, “intent” matching, answer quality.

## 12. Why embeddings are the next hypothesis

**Not** “embeddings are better.”  
**Because** the corpus includes paraphrase queries where lexical retrieval systematically misses relevant chunks that use different wording. That is a concrete failure mode for a semantic retrieval experiment to target — later, with measurement against this same corpus.

## Reconsideration triggers

- PG FTS latency unacceptable at real corpus size  
- Need true BM25 / different analyzer  
- Multi-tenant filters become mandatory product boundary  
- Lexical+structure saturates paraphrase failures (then embeddings)  
- Hybrid lexical+vector fusion required by eval

## Security / tenancy note

Today filters support `submission_id` / `artifact_key` / `ingestion_job_id`.  
Challenge/tenant isolation is a **future boundary** — document when product tenancy exists. Queries are bound parameters (no string-concat SQL injection).
