# Leaderboard consistency design

**Status:** KEEP — Postgres snapshot rows in the same TX as evaluation succeed  
**ADR:** `docs/adr/0016-leaderboard-consistency.md`

## 1. Problem

Succeeded evaluations store `score` (0–100), but there is no durable leaderboard,
publish gate, or deterministic tie-break. Live `ORDER BY evaluations.score` races
with in-flight workers and leaks ranks before the hackathon closes.

## 2. Industry options

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| Live query evaluations each GET | No new table | Races; no publish control | Reject as product default |
| Redis sorted set | Fast reads | Dual-write; not in stack | Reject |
| **Snapshot in same TX as succeed** | Authoritative; deterministic; visibility | Write amplification | **KEEP** |

## 3. Decision

1. Table `leaderboard_entries` keyed by `(challenge_id, submission_id)`.
2. Upsert on `EvaluationRepository.mark_succeeded` in the **same session/TX**.
3. Tie-break: `score DESC`, `completed_at ASC`, `submission_id ASC`.
4. Rank at read time with `RANK() OVER (...)` (ties share rank).
5. Visibility: organizer of hackathon always; participants only when hackathon is `closed`.
6. API: `GET /challenges/{challenge_id}/leaderboard`.

## 4. Non-goals

Outbox/webhooks, Redis, partial “publish results” flags (v1 uses hackathon `closed` only).
