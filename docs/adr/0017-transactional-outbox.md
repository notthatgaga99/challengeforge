# ADR 0017: Transactional outbox for domain notifications

## Status

Accepted.

## Context

Leaderboard snapshots and evaluation completion are durable in Postgres, but
side effects (webhooks/notifications) must not dual-write. Prior ADRs kept
queues in Postgres (0010, 0004); the same fit applies to outbound events.

## Decision

1. **KEEP** `outbox_events` written in the same TX as the business mutation.
2. Emit `evaluation.succeeded` on eval succeed; `hackathon.closed` on hackathon close.
3. Relay via `FOR UPDATE SKIP LOCKED` + pluggable `NotificationSink`.
4. Unique `idempotency_key`; at-least-once delivery; sink idempotency required.
5. Do **not** add Kafka/Redis/CDC for this milestone.

## Alternatives

| Option | Why not now |
|---|---|
| Sync notify after commit | Lost on crash |
| External queue dual-write | Same dual-write class |
| CDC pipeline | Ops overhead without scale need |

## Consequences

- Worker commit now also persists pending outbox rows (same TX as leaderboard).
- Closing a hackathon notifies “leaderboard published” without a second write path.
- Consumers must treat duplicate deliveries as no-ops.

## Related

ADR 0010 (ingestion jobs), ADR 0016 (leaderboard), ADR 0004 (eval queue).
