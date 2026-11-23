"""Outbox relay — claim pending events and deliver via NotificationSink."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from challengeforge.outbox.sinks import DeliveredEvent, LoggingNotificationSink, NotificationSink
from challengeforge.persistence.repositories import OutboxRepository


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker,
        sink: NotificationSink | None = None,
        *,
        batch_size: int = 10,
    ) -> None:
        self._factory = session_factory
        self._sink: NotificationSink = sink or LoggingNotificationSink()
        self._batch_size = batch_size
        self.published = 0
        self.failed = 0

    async def drain_once(self) -> int:
        """Claim up to batch_size pending events and attempt delivery.

        Returns number of successfully published events in this pass.
        Failed deliveries stay pending with attempt_count incremented.
        """
        async with self._factory() as session:
            repo = OutboxRepository(session)
            claimed = await repo.claim_pending(limit=self._batch_size)
            if not claimed:
                await session.commit()
                return 0
            published = 0
            for event in claimed:
                delivered = DeliveredEvent(
                    event_id=str(event.id),
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=str(event.aggregate_id),
                    idempotency_key=event.idempotency_key,
                    payload=event.payload,
                )
                try:
                    await self._sink.deliver(delivered)
                    await repo.mark_published(event.id)
                    published += 1
                    self.published += 1
                except Exception as exc:  # noqa: BLE001 — record and continue batch
                    await repo.mark_attempt_failed(event.id, str(exc))
                    self.failed += 1
            await session.commit()
            return published

    async def drain_all(self, *, max_passes: int = 100) -> int:
        """Drain until empty or max_passes (safety bound)."""
        total = 0
        for _ in range(max_passes):
            n = await self.drain_once()
            total += n
            if n == 0:
                break
        return total
