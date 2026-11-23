"""Outbox relay worker — poll pending events and deliver via NotificationSink."""

from __future__ import annotations

import argparse
import asyncio

import structlog
from sqlalchemy.ext.asyncio import async_sessionmaker

from challengeforge.config import Settings, get_settings
from challengeforge.outbox.factory import resolve_notification_sink
from challengeforge.outbox.relay import OutboxRelay
from challengeforge.outbox.sinks import NotificationSink
from challengeforge.persistence.session import dispose_engine, init_engine

logger = structlog.get_logger(__name__)


class OutboxWorker:
    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker | None = None,
        sink: NotificationSink | None = None,
        batch_size: int = 10,
    ) -> None:
        self.settings = settings
        self.stop_event = asyncio.Event()
        factory = session_factory or init_engine(settings)
        resolved = sink if sink is not None else resolve_notification_sink(settings)
        self.relay = OutboxRelay(factory, sink=resolved, batch_size=batch_size)
        self.passes = 0

    def request_stop(self) -> None:
        self.stop_event.set()

    async def run_once(self) -> int:
        published = await self.relay.drain_once()
        self.passes += 1
        return published

    async def run_forever(self) -> None:
        interval = self.settings.outbox_poll_interval_seconds
        while not self.stop_event.is_set():
            published = await self.run_once()
            if published:
                logger.info(
                    "outbox_relay_pass",
                    published=published,
                    total_published=self.relay.published,
                    total_failed=self.relay.failed,
                )
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass


async def _amain(settings: Settings) -> None:
    worker = OutboxWorker(settings)
    logger.info(
        "outbox_worker_start",
        webhook_configured=bool((settings.outbox_webhook_url or "").strip()),
        poll_interval_seconds=settings.outbox_poll_interval_seconds,
    )
    try:
        await worker.run_forever()
    finally:
        logger.info(
            "outbox_worker_stop",
            published=worker.relay.published,
            failed=worker.relay.failed,
            passes=worker.passes,
        )
        await dispose_engine()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="ChallengeForge outbox relay worker")
    parser.parse_args(argv)
    settings = get_settings()
    asyncio.run(_amain(settings))


if __name__ == "__main__":
    main()
