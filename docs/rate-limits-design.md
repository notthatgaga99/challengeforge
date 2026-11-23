# Rate limits / abuse budgets

**Status:** KEEP — Postgres fixed-window counters per actor+action  
**ADR:** `docs/adr/0020-rate-limits.md`

## 1. Problem

Submit/upload/search are authenticated but unbounded. A buggy client or abusive
participant can flood evaluation/ingestion/search. Redis token buckets are
industry-common; ChallengeForge stays Postgres-first.

## 2. Options

| Approach | Verdict |
|---|---|
| In-process counters | Reject — not shared across workers |
| Redis sliding window | Defer — new system |
| **Postgres fixed-window buckets** | **KEEP** |

## 3. Decision

1. Table `rate_limit_buckets` keyed by `(subject_id, action, window_started_at)`.
2. Atomic upsert +1; deny with `429` when `count > limit`.
3. Actions: `submission_create`, `artifact_upload`, `submission_submit`, `search`.
4. Settings-driven limits/window; `rate_limit_enabled` kill switch.
5. Idempotent submission create **replays do not consume** quota.
6. `RateLimited` carries `retry_after_seconds` for `Retry-After` header.

## 4. Non-goals

Per-IP anonymous limits, Redis, adaptive quotas, organizer exemptions (v1 same budgets).
