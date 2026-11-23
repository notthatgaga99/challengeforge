"""Transactional outbox — durable domain events + relay."""

from challengeforge.outbox.events import (
    EVENT_EVALUATION_SUCCEEDED,
    EVENT_HACKATHON_CLOSED,
)
from challengeforge.outbox.factory import resolve_notification_sink
from challengeforge.outbox.relay import OutboxRelay
from challengeforge.outbox.sinks import (
    HttpWebhookNotificationSink,
    LoggingNotificationSink,
    NotificationSink,
    RecordingNotificationSink,
    WebhookDeliveryError,
)
from challengeforge.outbox.worker import OutboxWorker

__all__ = [
    "EVENT_EVALUATION_SUCCEEDED",
    "EVENT_HACKATHON_CLOSED",
    "OutboxRelay",
    "OutboxWorker",
    "NotificationSink",
    "LoggingNotificationSink",
    "RecordingNotificationSink",
    "HttpWebhookNotificationSink",
    "WebhookDeliveryError",
    "resolve_notification_sink",
]
