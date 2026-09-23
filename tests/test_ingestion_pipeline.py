"""Deterministic ingestion pipeline reliability tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from challengeforge.config import Settings
from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    IngestionStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
from challengeforge.ingestion.processor import (
    DeterministicIngestError,
    process_artifact,
    result_key_for,
)
from challengeforge.ingestion.worker import IngestionWorker
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    Base,
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import IngestionJobRepository
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.storage.filesystem import LocalFilesystemStorage


@pytest.fixture
def storage(tmp_path: Path) -> LocalFilesystemStorage:
    return LocalFilesystemStorage(tmp_path / "artifacts")


async def _world(
    factory,
    storage: LocalFilesystemStorage,
    *,
    n_jobs: int = 1,
    body: bytes = b"hello",
):
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        hack = HackathonRow(
            id=uuid4(),
            title="H",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add(hack)
        chal = ChallengeRow(
            id=uuid4(),
            hackathon_id=hack.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add(chal)
        await session.flush()
        job_ids = []
        for i in range(n_jobs):
            sub = SubmissionRow(
                id=uuid4(),
                challenge_id=chal.id,
                participant_id=PARTICIPANT_ID,
                status=SubmissionStatus.CREATED.value,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(sub)
            await session.flush()
            key = f"submissions/{chal.id}/{PARTICIPANT_ID}/{i}.bin"
            payload = body if n_jobs == 1 else f"doc {i}".encode()
            storage.put(key, payload, "text/plain")
            job = await IngestionJobRepository(session).enqueue(
                submission_id=sub.id, artifact_key=key
            )
            job_ids.append(job.id)
        await session.commit()
        return job_ids


def test_process_idempotent_overwrite(storage: LocalFilesystemStorage):
    key = "submissions/t/a.bin"
    storage.put(key, b"hello world", "text/plain")
    job_id = uuid4()
    r1 = process_artifact(storage, job_id=job_id, artifact_key=key, attempt_count=1)
    r2 = process_artifact(storage, job_id=job_id, artifact_key=key, attempt_count=2)
    assert r1.result_key == r2.result_key == result_key_for(job_id)
    assert storage.get(r1.result_key) == storage.get(r2.result_key)


def test_poison_is_deterministic(storage: LocalFilesystemStorage):
    key = "submissions/t/poison.bin"
    storage.put(key, b"cf_ingest_poison bad", "text/plain")
    with pytest.raises(DeterministicIngestError) as ei:
        process_artifact(storage, job_id=uuid4(), artifact_key=key, attempt_count=1)
    assert ei.value.code == "malformed_artifact"


@pytest.mark.asyncio
async def test_ingestion_job_lifecycle(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "artifacts", ingestion_max_attempts=3)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"alpha beta")
    worker = IngestionWorker(
        worker_id="ing-1",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker.run_once() is True
    assert worker.succeeded == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        assert job is not None
        assert job.status == IngestionStatus.SUCCEEDED
        assert job.result_key and storage.exists(job.result_key)
        from challengeforge.persistence.repositories import DocumentChunkRepository

        chunks = await DocumentChunkRepository(session).list_for_job(job_ids[0])
        assert len(chunks) >= 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_poison_terminal_no_infinite_retry(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "art", ingestion_max_attempts=3)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"cf_ingest_poison xxx")
    worker = IngestionWorker(
        worker_id="ing-poison",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker.run_once()
    assert worker.failed == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        assert job.status == IngestionStatus.FAILED
        assert job.error_code == "malformed_artifact"
    assert await worker.run_once() is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_crash_after_process_then_stale_recovery_idempotent(
    database_url: str, tmp_path: Path
):
    engine = create_async_engine(database_url)
    settings = Settings(
        artifact_root=tmp_path / "art2",
        ingestion_max_attempts=3,
        ingestion_stale_after_seconds=0,
    )
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"durable content")
    job_id = job_ids[0]
    worker = IngestionWorker(
        worker_id="ing-crash",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    worker.crash_after_process = True
    with pytest.raises(RuntimeError, match="crash after process"):
        await worker.run_once()
    assert storage.exists(result_key_for(job_id))
    async with factory() as session:
        row = await session.get(IngestionJobRow, job_id)
        assert row is not None
        row.started_at = utcnow() - timedelta(seconds=60)
        await session.commit()
    assert await worker.recover_stale() >= 1
    worker2 = IngestionWorker(
        worker_id="ing-recover",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker2.run_once()
    assert worker2.succeeded == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_id)
        assert job.status == IngestionStatus.SUCCEEDED
        assert job.attempt_count >= 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_workers_no_double_success(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "art3", ingestion_max_attempts=3)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _world(factory, storage, n_jobs=20)
    workers = [
        IngestionWorker(
            worker_id=f"w{i}",
            session_factory=factory,
            storage=storage,
            settings=settings,
        )
        for i in range(4)
    ]

    async def drain(w: IngestionWorker) -> None:
        for _ in range(40):
            if not await w.run_once():
                break

    await asyncio.gather(*(drain(w) for w in workers))
    assert sum(w.succeeded for w in workers) == 20
    async with factory() as session:
        counts = await IngestionJobRepository(session).count_by_status()
        assert counts.get("succeeded") == 20
        assert counts.get("running", 0) == 0
        assert counts.get("queued", 0) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_transient_then_success(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(
        artifact_root=tmp_path / "art4",
        ingestion_max_attempts=3,
        ingestion_retry_delay_seconds=0.0,
    )
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"cf_ingest_transient x")
    worker = IngestionWorker(
        worker_id="ing-t",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker.run_once()
    assert worker.retried == 1
    assert await worker.run_once()
    assert worker.succeeded == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        assert job.status == IngestionStatus.SUCCEEDED
    await engine.dispose()


@pytest.mark.asyncio
async def test_invalid_encoding_terminal(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(artifact_root=tmp_path / "art5", ingestion_max_attempts=3)
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"\xff\xfe not utf8")
    worker = IngestionWorker(
        worker_id="ing-enc",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker.run_once()
    assert worker.failed == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        assert job.status == IngestionStatus.FAILED
        assert job.error_code == "invalid_encoding"
    await engine.dispose()


@pytest.mark.asyncio
async def test_claim_crash_then_stale_recovery(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    settings = Settings(
        artifact_root=tmp_path / "art6",
        ingestion_max_attempts=3,
        ingestion_stale_after_seconds=0,
    )
    storage = LocalFilesystemStorage(settings.artifact_root)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"claim crash")
    worker = IngestionWorker(
        worker_id="ing-cc",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    worker.crash_after_claim = True
    with pytest.raises(RuntimeError, match="crash after claim"):
        await worker.run_once()
    async with factory() as session:
        row = await session.get(IngestionJobRow, job_ids[0])
        assert row.status == "running"
        row.started_at = utcnow() - timedelta(seconds=60)
        await session.commit()
    assert await worker.recover_stale() >= 1
    worker2 = IngestionWorker(
        worker_id="ing-cc2",
        session_factory=factory,
        storage=storage,
        settings=settings,
    )
    assert await worker2.run_once()
    assert worker2.succeeded == 1
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        assert job.status == IngestionStatus.SUCCEEDED
    await engine.dispose()


@pytest.mark.asyncio
async def test_job_remains_queued_without_worker(database_url: str, tmp_path: Path):
    engine = create_async_engine(database_url)
    storage = LocalFilesystemStorage(tmp_path / "art7")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    job_ids = await _world(factory, storage, body=b"parked")
    async with factory() as session:
        job = await IngestionJobRepository(session).get(job_ids[0])
        counts = await IngestionJobRepository(session).count_by_status()
        assert job.status == IngestionStatus.QUEUED
        assert counts == {"queued": 1}
    await engine.dispose()
