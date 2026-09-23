# ADR 0011: Chunking & representation

## Status

Accepted: **Hybrid structure-aware chunking with a Unicode character budget**,
persisted in PostgreSQL `document_chunks`, versioned by parser/chunker.

## Context

Ingestion produced a READY manifest but no retrieval-oriented units. Fixed
splitting is insufficient for Markdown/code. Token budgets require a model
choice ChallengeForge has not made. Vector databases are out of scope.

Prior synthetic normalize collapsed whitespace and destroyed structure; canonical
text must preserve line structure for provenance.

## Decision

1. Canonicalize with `cf-parse-2` (CRLF→LF, strip trailing spaces, keep structure).  
2. Chunk with `cf-chunk-hybrid-1`: structure units → hard-split oversized → pack
   contiguous spans under `ingestion_chunk_max_chars` (default 1200).  
3. Overlap = 0 so each chunk equals `canonical[char_start:char_end]`.  
4. Persist chunks in Postgres keyed by `ingestion_job_id` + `ordinal`; stamp
   `parser_version` / `chunker_version` / `content_sha256`.  
5. Mark ingestion succeeded only in the same transaction as chunk replace.  
6. Do not introduce tokenizers, embeddings, or vector stores.

## Alternatives

| Option | Why not now |
|---|---|
| Fixed-size only | Mid-structure splits; no heading context |
| Token budget | Premature without embedding/chat model |
| Structure-only | Unbounded pathological units |
| Recursive hierarchy store | Extra complexity before retrieval evidence |
| Derive on read | Weak for citations/debug; recomputes versions unsafely |

## Evidence

- `docs/chunking-and-representation.md`  
- `scripts/chunking_representation_experiment.py`  
- `tests/test_chunking_representation.py`  
- Migration `0012_document_chunks`

## Trade-offs

**+** Deterministic, provenance-exact, versioned, durable, Postgres-native  
**−** Character ≠ token; no overlap may hurt some retrievers later; heuristic
Markdown/code unitization (not a full AST)

## Reconsideration

Embedding model selected · boundary failures in retrieval evals · symbol-level
code needs · corpus scale outgrows Postgres rows · need raw-byte provenance.

## Related

ADR 0010 (ingestion durability), ADR 0009 (artifact consistency).
