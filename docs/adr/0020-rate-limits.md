# ADR 0020: Postgres fixed-window rate limits

## Status

Accepted.

## Context

Product APIs for submissions, uploads, and search lacked abuse budgets. Prior
ADRs kept durability in Postgres; rate limiting should fit the same constraint.

## Decision

1. **KEEP** fixed-window counters in `rate_limit_buckets`.
2. Enforce in application services (create/upload/submit/search).
3. Return `429 rate_limited` with `Retry-After`.
4. Disable via `RATE_LIMIT_ENABLED=false` for load experiments.
5. Do **not** add Redis for this milestone.

## Alternatives

| Option | Why not |
|---|---|
| In-memory only | Multi-worker incorrect |
| Redis | Extra ops system |

## Related

ADR 0001 (monolith), submission idempotency, search tenancy.
