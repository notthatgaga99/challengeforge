# ADR 0016: Leaderboard consistency (snapshot on evaluation succeed)

## Status

Accepted.

## Context

Judging needs stable ranks. Reading live evaluation rows has no publish gate and
races with workers. ChallengeForge already completes evaluations in Postgres
transactions.

## Decision

1. **KEEP** `leaderboard_entries` snapshots upserted in the same TX as
   `mark_succeeded`.
2. Deterministic order: score DESC, completed_at ASC, submission_id ASC.
3. Visibility: owning organizer always; participants when hackathon status is
   `closed`.
4. `GET /challenges/{id}/leaderboard` with `RANK() OVER`.
5. Do **not** introduce Redis or an outbox for v1.

## Alternatives

| Option | Why not now |
|---|---|
| Live ORDER BY evaluations | Race + no publish gate |
| Redis ZSET | Dual-write; extra system |
| Async outbox projection | Correct later; more moving parts than needed |

## Consequences

- Failures do not publish winning rows; succeed overwrites the snapshot.
- Closing a hackathon is the participant publish gate (service helper added).

## Related

ADR 0003/0004 (evaluation queue), ADR 0015 (search tenancy / authz patterns).
