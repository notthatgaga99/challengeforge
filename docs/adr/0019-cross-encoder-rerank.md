# ADR 0019: Cross-encoder rerank over hybrid candidates

## Status

Accepted as **implemented candidate** `cf-hybrid-rerank-1`.  
**HOLD promote** — curriculum overlap bakeoff MRR 0.81 < hybrid 0.95
(`docs/rerank-bakeoff-results.json`). Live default remains `cf-hybrid-rrf-1`.

## Context

Hybrid closed paraphrase recall. Ranking-quality failures with good recall are
the classic trigger for a cross-encoder stage.

## Decision

1. Add `cf-hybrid-rerank-1` = hybrid shortlist + `RerankProvider`.
2. Curriculum `LexicalOverlapRerankProvider` for CI/offline demos.
3. Optional MiniLM-style CrossEncoder behind the same interface when installed.
4. Do **not** change `retrieval_default_version` without measured bakeoff KEEP.

## Alternatives

| Option | Why not |
|---|---|
| Cross-encoder first | Latency/cost without candidate gen |
| Always-on live rerank | Needs quality evidence + cost budget |

## Related

ADR 0013, 0018; `docs/cross-encoder-rerank-design.md`.
