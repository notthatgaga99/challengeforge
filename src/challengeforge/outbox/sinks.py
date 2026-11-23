"""Pluggable notification sinks for outbox delivery."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import structlog

logger = structlog.get_logger(__name__)

SECRET_HEADER = "X-ChallengeForge-Secret"


@dataclass(frozen=True)
class DeliveredEvent:
    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    idempotency_key: str
    payload: dict[str, Any]


class NotificationSink(Protocol):
    async def deliver(self, event: DeliveredEvent) -> None:
        """Deliver once logically; must be safe under at-least-once retries."""


class LoggingNotificationSink:
    """Dev default — logs structured delivery (no external I/O)."""

    async def deliver(self, event: DeliveredEvent) -> None:
        logger.info(
            "outbox_delivered",
            event_id=event.event_id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            idempotency_key=event.idempotency_key,
        )


@dataclass
class RecordingNotificationSink:
    """Test sink — records deliveries; optional failure injection."""

    delivered: list[DeliveredEvent] = field(default_factory=list)
    seen_keys: set[str] = field(default_factory=set)
    fail_times: int = 0
    _failures_remaining: int = field(init=False, default=0)

    def __post_init__(self) -> None:
        self._failures_remaining = self.fail_times

    async def deliver(self, event: DeliveredEvent) -> None:
        if self._failures_remaining > 0:
            self._failures_remaining -= 1
            raise RuntimeError("injected sink failure")
        # Idempotent: duplicate key is a no-op after first success.
        if event.idempotency_key in self.seen_keys:
            return
        self.seen_keys.add(event.idempotency_key)
        self.delivered.append(event)


class WebhookDeliveryError(RuntimeError):
    """Raised when the webhook receiver rejects or is unreachable."""


@dataclass
class HttpWebhookNotificationSink:
    """POST DeliveredEvent JSON to a configured URL (ADR 0021).

    At-least-once: receivers must dedupe on ``Idempotency-Key``.
    """

    url: str
    timeout_seconds: float = 5.0
    secret: str | None = None
    client: httpx.AsyncClient | None = None

    async def deliver(self, event: DeliveredEvent) -> None:
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": event.idempotency_key,
        }
        if self.secret:
            headers[SECRET_HEADER] = self.secret
        body = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "aggregate_type": event.aggregate_type,
            "aggregate_id": event.aggregate_id,
            "idempotency_key": event.idempotency_key,
            "payload": event.payload,
        }
        owns_client = self.client is None
        client = self.client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds)
        )
        try:
            response = await client.post(self.url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise WebhookDeliveryError(f"webhook transport error: {exc}") from exc
        finally:
            if owns_client:
                await client.aclose()
        if response.status_code < 200 or response.status_code >= 300:
            raise WebhookDeliveryError(
                f"webhook HTTP {response.status_code}: {response.text[:500]}"
            )
