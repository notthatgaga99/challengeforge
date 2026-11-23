# Promote hybrid RRF as live retrieval default

**Status:** KEEP — `cf-hybrid-rrf-1` via settings; lexical rollback by env  
**ADR:** `docs/adr/0018-promote-hybrid-default.md`

## 1. Context

Hybrid RRF was the candidate default (ADR 0013) after measured paraphrase lift.
MiniLM bakeoff (ADR 0014) preferred a real encoder but kept live lexical.
Search tenancy (ADR 0015) is in place. Remaining gap: product search still
hard-coded `cf-lex-fts-1` and did not pass an embedding provider.

## 2. Options

| Approach | Verdict |
|---|---|
| Flip hard-code only | Reject — no rollback |
| New microservice | Reject — monolith |
| **Settings default + provider resolve + ingest-time embed** | **KEEP** |

## 3. Decision

1. `Settings.retrieval_default_version = "cf-hybrid-rrf-1"`.
2. Env override `RETRIEVAL_DEFAULT_VERSION=cf-lex-fts-1` rolls back instantly.
3. `Settings.retrieval_embedding_provider = curriculum|minilm|stub` (default curriculum).
4. Product search resolves provider when version is dense/hybrid.
5. Ingestion success path indexes embeddings in the same TX as chunk persist.
6. Clients may omit `retrieval_version` (use default) or override explicitly.
