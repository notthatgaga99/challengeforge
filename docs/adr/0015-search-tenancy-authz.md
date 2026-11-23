# ADR 0015: Search tenancy / authz (deny-by-default SQL scope)

## Status

Accepted.

## Context

Semantic/lexical retrieval is measured, but filters lacked hackathon/challenge/actor
scope. Before any live search API, cross-tenant leakage must be impossible by
construction.

## Decision

1. **KEEP** deny-by-default **effective** `RetrievalFilters` injected by
   `resolve_retrieval_scope` on the product search path.
2. Enforce scope in SQL via joins to `submissions` / `challenges` (not post-filter).
3. Organizers: owned hackathon only. Participants: own submissions only.
4. Expose `POST /hackathons/{id}/search` with `CurrentUser` (`X-User-Id`).
5. Do **not** rely on Postgres RLS yet. Do **not** change live retrieval default version.

## Alternatives

| Option | Why not now |
|---|---|
| App-layer filter after retrieve | Leak risk; over-fetch |
| RLS | Curriculum opacity; harder local tests |
| Unscoped global search | Unacceptable for judging platform |

## Consequences

- Dense/hybrid inherit the same `_filter_clauses` joins.
- Experiments remain unscoped unless they pass filters explicitly.
- Leaderboard visibility is a separate ADR (0016).

## Related

ADR 0012–0014 (retrieval), ADR 0001 (modular monolith), identity via `CurrentUser`.
