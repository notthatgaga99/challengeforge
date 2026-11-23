# Cross-encoder rerank on hybrid candidates

**Status:** Measured candidate — live default stays `cf-hybrid-rrf-1` unless bakeoff wins  
**ADR:** `docs/adr/0019-cross-encoder-rerank.md`

## 1. Problem

Hybrid RRF optimizes *candidate generation* (recall). Pairwise relevance ranking
can still be wrong when dense and lexical disagree. Industry second stage:
cross-encoder scores `(query, chunk)` jointly over a shortlist.

## 2. Options

| Approach | Verdict |
|---|---|
| Cross-encoder as sole retriever | Reject — too slow at corpus scale |
| Weighted score soup | Reject — incomparable score spaces |
| **Hybrid candidates → cross-encoder rerank** | **KEEP** |

## 3. Decision

1. Version `cf-hybrid-rerank-1`: hybrid RRF top-`candidate_k` → rerank → top_k.
2. `RerankProvider` interface; curriculum overlap scorer for offline/CI;
   optional real CrossEncoder when `sentence-transformers` installed.
3. Live default remains hybrid (ADR 0018) until measured promote.
4. Same tenancy filters as hybrid.

## 4. Non-goals

Flipping live default without bakeoff; dedicated GPU serving; learning-to-rank.
