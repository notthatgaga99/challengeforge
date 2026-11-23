# ADR 0014: MiniLM vs curriculum embedding bakeoff

## Status

Accepted (measured). Does **not** change the live retrieval default.

## Context

ADR 0013 introduced dense/hybrid retrieval with a curriculum concept encoder
that demonstrates paraphrase geometry offline. That does **not** prove a real
bi-encoder. This milestone runs `all-MiniLM-L6-v2` against the same judged
corpus and metrics.

## Decision

1. **KEEP** `local:all-MiniLM-L6-v2:384:cosine` as the preferred *real* encoder
   for candidate hybrid (`cf-hybrid-rrf-1`).
2. **KEEP** `curriculum:concept-v1` for offline/CI semantic demos without torch.
3. **KEEP** live default `cf-lex-fts-1` until an explicit promote milestone.
4. Optional install: `pip install '.[embeddings]'` (`sentence-transformers`).

## Evidence

See `docs/minilm-bakeoff-results.json` (script: `scripts/minilm_bakeoff_experiment.py`).

| Arm | Dense MRR | Hybrid MRR | Paraphrase zero-hits (hybrid) |
|---|---:|---:|---:|
| Lexical control | — | 0.75 (lex MRR) | 2 |
| Curriculum | 0.90 | 1.00 | 0 |
| MiniLM | 0.80 | 0.90 | 0 |

Curriculum edges MiniLM on this tiny, jargon-tuned corpus (expected). MiniLM
still closes the lexical paraphrase gap and is the honest production-shaped encoder.

## Consequences

- Do not claim “MiniLM worse than curriculum” in general — the curriculum encoder
  is educational and corpus-aligned.
- Promoting hybrid to live default still requires product review + tenancy/authz.
- Heavier deps stay optional so CI/dev without embeddings keep working.

## Reconsideration triggers

- Larger / less curated corpus where MiniLM clearly wins  
- Identifier regressions under MiniLM hybrid  
- Desire to flip live default → promote milestone  
- Stronger local models (E5/BGE) bakeoff  
