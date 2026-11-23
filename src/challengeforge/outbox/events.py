"""Outbox event type constants and idempotency key helpers."""

from __future__ import annotations

from uuid import UUID

EVENT_EVALUATION_SUCCEEDED = "evaluation.succeeded"
EVENT_HACKATHON_CLOSED = "hackathon.closed"


def evaluation_succeeded_key(evaluation_id: UUID) -> str:
    return f"{EVENT_EVALUATION_SUCCEEDED}:{evaluation_id}"


def hackathon_closed_key(hackathon_id: UUID) -> str:
    return f"{EVENT_HACKATHON_CLOSED}:{hackathon_id}"
