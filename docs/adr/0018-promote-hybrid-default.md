# ADR 0018: Promote hybrid RRF as live retrieval default

## Status

Accepted (feature-flagged via settings).

## Context

Measured hybrid lift (ADR 0013/0014), tenancy ready (ADR 0015). Live API still
defaulted to lexical and lacked embedding provider wiring.

## Decision

1. **KEEP** product default `retrieval_default_version=cf-hybrid-rrf-1`.
2. Rollback: set env `RETRIEVAL_DEFAULT_VERSION=cf-lex-fts-1` (no code deploy).
3. Default product encoder: `curriculum` (offline-safe); optional `minilm`/`stub`.
4. Index embeddings during ingestion succeed (same TX as chunks).
5. SearchService uses settings default when client omits version.

## Evidence

Prior results JSON + MiniLM bakeoff; tenancy ADR 0015.

## Consequences

- Hybrid without embeddings still returns lexical-only RRF lists (degraded).
- With ingest indexing, dense contributes on new documents.
- Client override of version remains allowed for debugging.

## Related

ADR 0013–0015, `docs/promote-hybrid-default-design.md`.
