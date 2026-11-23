# Outbox HTTP webhook sink

**Status:** KEEP — httpx `HttpWebhookNotificationSink` behind `NotificationSink`  
**ADR:** `docs/adr/0021-outbox-webhook-sink.md`  
**Builds on:** ADR 0017 (`outbox_events` + `OutboxRelay`)

## 1. Problem

ADR 0017 made domain notifications durable and retryable, but the live sink was
logging/recording only. Product webhooks need real HTTP delivery without
abandoning Postgres-first constraints.

## 2. Options

| Approach | Verdict |
|---|---|
| Sync notify in business TX | Reject — dual-write |
| Kafka / SQS fan-out | Defer — new system |
| **HTTP POST sink + existing relay** | **KEEP** |

## 3. Decision

1. `HttpWebhookNotificationSink` POSTs JSON mirroring `DeliveredEvent`.
2. Headers: `Idempotency-Key` (required for receiver dedupe); optional
   `X-ChallengeForge-Secret` when configured.
3. HTTP 2xx → success; timeout / network / non-2xx → raise (row stays pending).
4. Settings: `OUTBOX_WEBHOOK_URL` (empty → logging sink), timeout, secret.
5. Thin outbox relay worker polls independently of the evaluation worker.
6. No schema change; no Kafka; no exponential-backoff table (re-claim is enough).

## 4. Non-goals

Signed payloads beyond shared secret, DLQ table, exactly-once broker semantics.
