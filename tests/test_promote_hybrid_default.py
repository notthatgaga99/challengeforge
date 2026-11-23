"""Promote hybrid RRF as live default (ADR 0018) — flag, override, embed path."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from challengeforge.application import UnitOfWork
from challengeforge.application.search import SearchService
from challengeforge.config import Settings
from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
    UserRole,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID, CurrentUser
from challengeforge.ingestion.canonical import PARSER_VERSION, normalize_to_canonical
from challengeforge.ingestion.chunking import CHUNKER_VERSION, chunk_hybrid
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    ChallengeRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import DocumentChunkRepository
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.retrieval.dense import (
    RETRIEVAL_VERSION_DENSE,
    RETRIEVAL_VERSION_HYBRID_RRF,
    index_chunks,
)
from challengeforge.retrieval.embeddings import (
    CurriculumConceptEmbeddingProvider,
    resolve_product_embedding_provider,
    version_requires_embeddings,
)
from challengeforge.retrieval.lexical import RETRIEVAL_VERSION_FTS
from challengeforge.storage.filesystem import LocalFilesystemStorage
from tests.test_semantic_retrieval import _prepare


def test_settings_default_is_hybrid():
    assert Settings().retrieval_default_version == RETRIEVAL_VERSION_HYBRID_RRF


def test_settings_rollback_via_env(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_DEFAULT_VERSION", RETRIEVAL_VERSION_FTS)
    Settings.model_config  # touch
    s = Settings()
    assert s.retrieval_default_version == RETRIEVAL_VERSION_FTS


def test_version_requires_embeddings():
    assert version_requires_embeddings(RETRIEVAL_VERSION_HYBRID_RRF)
    assert version_requires_embeddings(RETRIEVAL_VERSION_DENSE)
    assert not version_requires_embeddings(RETRIEVAL_VERSION_FTS)


def test_resolve_product_provider_curriculum():
    p = resolve_product_embedding_provider("curriculum")
    assert isinstance(p, CurriculumConceptEmbeddingProvider)


def test_search_service_resolves_default_and_override():
    settings = Settings(retrieval_default_version=RETRIEVAL_VERSION_HYBRID_RRF)
    uow = UnitOfWork(
        session=None,  # type: ignore[arg-type]
        storage=LocalFilesystemStorage(Path(".")),
        settings=settings,
    )
    svc = SearchService(uow)
    assert svc.resolve_retrieval_version(None) == RETRIEVAL_VERSION_HYBRID_RRF
    assert svc.resolve_retrieval_version(RETRIEVAL_VERSION_FTS) == RETRIEVAL_VERSION_FTS


async def _seed_searchable(factory, *, text: str):
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
        job_id = uuid4()
        session.add(
            IngestionJobRow(
                id=job_id,
                submission_id=sub.id,
                artifact_key="a.md",
                status="succeeded",
                attempt_count=1,
                worker_id="t",
                available_at=now,
                started_at=now,
                completed_at=now,
                error_code=None,
                error_message=None,
                result_key=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
        drafts = chunk_hybrid(normalize_to_canonical(text), max_chars=1200)
        chunks = await DocumentChunkRepository(session).replace_for_job(
            ingestion_job_id=job_id,
            submission_id=sub.id,
            artifact_key="a.md",
            parser_version=PARSER_VERSION,
            chunker_version=CHUNKER_VERSION,
            chunks=drafts,
        )
        provider = CurriculumConceptEmbeddingProvider()
        await index_chunks(session, provider, chunks)
        await session.commit()
        return hack.id


@pytest.mark.asyncio
async def test_product_search_defaults_to_hybrid_with_embeddings():
    engine, factory = await _prepare()
    try:
        hack_id = await _seed_searchable(
            factory,
            text=(
                "# Worker recovery\n\n"
                "stale RUNNING evaluations are requeued after worker failure.\n"
            ),
        )
        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(
                    retrieval_default_version=RETRIEVAL_VERSION_HYBRID_RRF,
                    retrieval_embedding_provider="curriculum",
                ),
            )
            svc = SearchService(uow)
            org = CurrentUser(
                id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER
            )
            hits, version = await svc.search_hackathon(
                org,
                hack_id,
                query="how does the system recover work after a worker crashes?",
            )
            assert version == RETRIEVAL_VERSION_HYBRID_RRF
            assert len(hits) >= 1
            assert hits[0].retrieval_version == RETRIEVAL_VERSION_HYBRID_RRF
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_client_can_override_to_lexical():
    engine, factory = await _prepare()
    try:
        hack_id = await _seed_searchable(
            factory,
            text="# Claiming\n\nWorkers claim using FOR UPDATE SKIP LOCKED.\n",
        )
        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(
                    retrieval_default_version=RETRIEVAL_VERSION_HYBRID_RRF
                ),
            )
            svc = SearchService(uow)
            org = CurrentUser(
                id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER
            )
            hits, version = await svc.search_hackathon(
                org,
                hack_id,
                query="SKIP LOCKED",
                retrieval_version=RETRIEVAL_VERSION_FTS,
            )
            assert version == RETRIEVAL_VERSION_FTS
            assert len(hits) >= 1
            assert all(h.retrieval_version == RETRIEVAL_VERSION_FTS for h in hits)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rolled_back_settings_use_lexical_default():
    engine, factory = await _prepare()
    try:
        hack_id = await _seed_searchable(
            factory,
            text="# Idempotency\n\nRetries must be idempotent for uploads.\n",
        )
        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(retrieval_default_version=RETRIEVAL_VERSION_FTS),
            )
            svc = SearchService(uow)
            org = CurrentUser(
                id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER
            )
            hits, version = await svc.search_hackathon(
                org, hack_id, query="idempotent"
            )
            assert version == RETRIEVAL_VERSION_FTS
            assert len(hits) >= 1
    finally:
        await engine.dispose()
