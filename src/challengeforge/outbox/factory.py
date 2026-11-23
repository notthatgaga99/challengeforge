"""Resolve the configured outbox NotificationSink."""

from __future__ import annotations

from challengeforge.config import Settings
from challengeforge.outbox.sinks import (
    HttpWebhookNotificationSink,
    LoggingNotificationSink,
    NotificationSink,
)


def resolve_notification_sink(settings: Settings) -> NotificationSink:
    """URL set → HTTP webhook; otherwise structured logging (dev default)."""
    url = (settings.outbox_webhook_url or "").strip()
    if not url:
        return LoggingNotificationSink()
    return HttpWebhookNotificationSink(
        url=url,
        timeout_seconds=settings.outbox_webhook_timeout_seconds,
        secret=(settings.outbox_webhook_secret or None),
    )
