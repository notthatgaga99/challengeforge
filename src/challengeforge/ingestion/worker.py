"""Ingestion worker: claim → process → persist chunks → succeed/fail/retry.

PostgreSQL queue with FOR UPDATE SKIP LOCKED. At-least-once execution;
idempotent effects via stable blob keys + replace_for_job chunk writes.

Durable success requires: result.json + canonical.txt + document_chunks rows
committed in the same DB transaction as status=succeeded.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from challengeforge.config import Settings
from challengeforge.ingestion.chunking import CHUNKER_VERSION, DEFAULT_MAX_CHARS
from challengeforge.ingestion.processor import (
    DeterministicIngestError,
    TransientIngestError,
    cleanup_partial_result,
    process_artifact,
)
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.repositories import (
    DocumentChunkRepository,
    IngestionJobRepository,
)
from challengeforge.storage.base import ArtifactStorage

log = logging.getLogger(__name__)


class IngestionWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ArtifactStorage,
        settings: Settings,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.session_factory = session_factory
        self.storage = storage
        self.settings = settings
        self.stop_event = stop_event or asyncio.Event()
        self.claims = 0
        self.succeeded = 0
        self.failed = 0
        self.retried = 0
        self.crash_after_claim = False
        self.crash_after_process = False
        self.crash_after_chunk_persist = False
        self._on_claimed: Callable[[UUID], None] | None = None

    def request_stop(self) -> None:
        self.stop_event.set()

    async def _cleanup_job_outputs(self, job_id: UUID) -> None:
        cleanup_partial_result(self.storage, job_id)
        async with self.session_factory() as session:
            await DocumentChunkRepository(session).delete_for_job(job_id)
            await session.commit()

    async def recover_stale(self) -> int:
        stale_before = utcnow() - timedelta(
            seconds=self.settings.ingestion_stale_after_seconds
        )
        async with self.session_factory() as session:
            repo = IngestionJobRepository(session)
            recovered = await repo.recover_stale_running(
                stale_before=stale_before,
                max_attempts=self.settings.ingestion_max_attempts,
            )
            await session.commit()
            for job in recovered:
                if job.status.value == "queued":
                    await self._cleanup_job_outputs(job.id)
            return len(recovered)

    async def run_once(self) -> bool:
        """Claim and process one job. Returns True if work was done."""
        async with self.session_factory() as session:
            repo = IngestionJobRepository(session)
            claimed = await repo.claim_next(worker_id=self.worker_id)
            await session.commit()
        if claimed is None:
            return False

        self.claims += 1
        if self._on_claimed is not None:
            self._on_claimed(claimed.id)
        if self.crash_after_claim:
            raise RuntimeError("simulated crash after claim")

        max_chars = int(
            getattr(self.settings, "ingestion_chunk_max_chars", DEFAULT_MAX_CHARS)
        )
        try:
            result = process_artifact(
                self.storage,
                job_id=claimed.id,
                artifact_key=claimed.artifact_key,
                attempt_count=claimed.attempt_count,
                max_chunk_chars=max_chars,
                chunker_version=CHUNKER_VERSION,
            )
        except DeterministicIngestError as exc:
            async with self.session_factory() as session:
                repo = IngestionJobRepository(session)
                await DocumentChunkRepository(session).delete_for_job(claimed.id)
                await repo.mark_failed(
                    job_id=claimed.id,
                    worker_id=self.worker_id,
                    error_code=exc.code,
                    error_message=exc.message,
                )
                await session.commit()
            cleanup_partial_result(self.storage, claimed.id)
            self.failed += 1
            return True
        except TransientIngestError as exc:
            await self._handle_retryable(claimed, exc.code, exc.message)
            return True
        except Exception as exc:
            log.exception("ingestion_unexpected_error job_id=%s", claimed.id)
            await self._handle_retryable(claimed, "executor_error", str(exc))
            return True

        if self.crash_after_process:
            raise RuntimeError("simulated crash after process before ack")

        async with self.session_factory() as session:
            chunks = DocumentChunkRepository(session)
            jobs = IngestionJobRepository(session)
            await chunks.replace_for_job(
                ingestion_job_id=claimed.id,
                submission_id=claimed.submission_id,
                artifact_key=claimed.artifact_key,
                parser_version=result.parser_version,
                chunker_version=result.chunker_version,
                chunks=result.chunks,
            )
            if self.crash_after_chunk_persist:
                await session.commit()
                raise RuntimeError("simulated crash after chunk persist before succeed")
            done = await jobs.mark_succeeded(
                job_id=claimed.id,
                worker_id=self.worker_id,
                result_key=result.result_key,
            )
            await session.commit()
        if done is None:
            return True
        self.succeeded += 1
        return True

    async def _handle_retryable(self, claimed, code: str, message: str) -> None:
        if claimed.attempt_count >= self.settings.ingestion_max_attempts:
            async with self.session_factory() as session:
                await DocumentChunkRepository(session).delete_for_job(claimed.id)
                await IngestionJobRepository(session).mark_failed(
                    job_id=claimed.id,
                    worker_id=self.worker_id,
                    error_code=code,
                    error_message=message + " (attempts exhausted)",
                )
                await session.commit()
            cleanup_partial_result(self.storage, claimed.id)
            self.failed += 1
        else:
            async with self.session_factory() as session:
                await DocumentChunkRepository(session).delete_for_job(claimed.id)
                await IngestionJobRepository(session).release_for_retry(
                    job_id=claimed.id,
                    worker_id=self.worker_id,
                    error_code=code,
                    error_message=message,
                    delay_seconds=self.settings.ingestion_retry_delay_seconds,
                )
                await session.commit()
            cleanup_partial_result(self.storage, claimed.id)
            self.retried += 1

    async def run_forever(self) -> None:
        while not self.stop_event.is_set():
            await self.recover_stale()
            worked = await self.run_once()
            if not worked:
                try:
                    await asyncio.wait_for(
                        self.stop_event.wait(),
                        timeout=self.settings.ingestion_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
