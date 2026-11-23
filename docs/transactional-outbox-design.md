# Transactional outbox for notifications

**Status:** KEEP — Postgres `outbox_events` + poll relay + pluggable sink  
**ADR:** `docs/adr/0017-transactional-outbox.md`

## 1. Problem

Evaluation succeed and hackathon close already mutate durable state (evaluation,
leaderboard). Calling a webhook/email **in the same request/worker after commit**
is a classic dual-write: DB committed, process dies, notification never sent —
or notification sent, DB rolls back. ChallengeForge needs at-least-once delivery
of domain events without Redis/Kafka.

## 2. Industry options

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| Sync HTTP after commit | Simple | Lost on crash; no retry ledger | Reject |
| Dual-write DB + queue | Familiar | Dual-write failure modes | Reject |
| CDC (Debezium) | Strong | Ops-heavy; not laptop curriculum | Defer |
| **Transactional outbox + relay** | Same TX as business write; retryable | Poller latency; at-least-once | **KEEP** |

## 3. Decision

1. Table `outbox_events`: pending until `published_at` set.
2. Insert events in the **same session/TX** as:
   - `evaluation.succeeded` from `EvaluationRepository.mark_succeeded`
   - `hackathon.closed` from `HackathonService.close_hackathon`
3. Unique `idempotency_key` per logical event (prevents duplicate rows on retry).
4. `OutboxRelay.drain_once()` claims with `FOR UPDATE SKIP LOCKED`, delivers via
   `NotificationSink`, marks published (or records attempt/error).
5. Default sink: `RecordingNotificationSink` (tests) / `LoggingNotificationSink`
   (dev). Real HTTP webhooks plug the same interface later.
6. Delivery is **at-least-once**; sinks must be idempotent on `idempotency_key`.

## 4. Non-goals

Kafka, Debezium, exactly-once broker semantics, product email providers.
