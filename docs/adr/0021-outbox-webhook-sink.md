# ADR 0021: HTTP webhook sink for transactional outbox

## Status

Accepted.

## Context

ADR 0017 introduced `outbox_events` and a pluggable `NotificationSink`, but
delivery stayed in-process (logging/recording). Real receivers need HTTP.

## Decision

1. **KEEP** `HttpWebhookNotificationSink` (httpx) as an optional sink.
2. Configure via `OUTBOX_WEBHOOK_URL`; unset URL keeps `LoggingNotificationSink`.
3. Send `Idempotency-Key` on every POST; receivers must treat duplicates as no-ops.
4. Non-2xx / transport errors leave the row pending (`attempt_count` + `last_error`).
5. Do **not** add Kafka, CDC, or a separate retry schedule table for this milestone.

## Alternatives

| Option | Why not |
|---|---|
| Sync HTTP after business commit | Lost on crash |
| Message bus | Ops surface without scale need |

## Related

ADR 0017 (transactional outbox), ADR 0001 (modular monolith).
