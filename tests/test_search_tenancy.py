"""Search tenancy / authz — deny-by-default SQL scope."""

from __future__ import annotations

from uuid import uuid4

import pytest

from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
    UserRole,
)
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied
from challengeforge.domain.models import Hackathon
from challengeforge.identity import (
    ORGANIZER_ID,
    PARTICIPANT_2_ID,
    PARTICIPANT_ID,
    CurrentUser,
)
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
from challengeforge.retrieval.authz import resolve_retrieval_scope
from challengeforge.retrieval.lexical import retrieve
from tests.test_semantic_retrieval import _prepare


def _hack(
    *,
    organizer_id=ORGANIZER_ID,
    status=HackathonStatus.PUBLISHED,
    hackathon_id=None,
) -> Hackathon:
    now = utcnow()
    hid = hackathon_id or uuid4()
    return Hackathon(
        id=hid,
        title="H",
        description="d",
        status=status,
        organizer_id=organizer_id,
        created_at=now,
        updated_at=now,
    )


def test_organizer_scope_requires_ownership():
    hack = _hack(organizer_id=ORGANIZER_ID)
    other = CurrentUser(
        id=uuid4(), display_name="X", role=UserRole.ORGANIZER
    )
    with pytest.raises(PermissionDenied):
        resolve_retrieval_scope(other, hack)
    filters = resolve_retrieval_scope(
        CurrentUser(id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER),
        hack,
    )
    assert filters.hackathon_id == hack.id
    assert filters.participant_id is None


def test_participant_always_scoped_to_self():
    hack = _hack()
    filters = resolve_retrieval_scope(
        CurrentUser(id=PARTICIPANT_ID, display_name="Pat", role=UserRole.PARTICIPANT),
        hack,
        challenge_id=uuid4(),
        challenge_hackathon_id=hack.id,
    )
    assert filters.participant_id == PARTICIPANT_ID
    assert filters.hackathon_id == hack.id


def test_participant_cannot_see_draft_hackathon():
    hack = _hack(status=HackathonStatus.DRAFT)
    with pytest.raises(NotFoundError):
        resolve_retrieval_scope(
            CurrentUser(
                id=PARTICIPANT_ID, display_name="Pat", role=UserRole.PARTICIPANT
            ),
            hack,
        )


def test_challenge_must_belong_to_hackathon():
    hack = _hack()
    with pytest.raises(PermissionDenied):
        resolve_retrieval_scope(
            CurrentUser(id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER),
            hack,
            challenge_id=uuid4(),
            challenge_hackathon_id=uuid4(),
        )


async def _seed_two_tenants(factory):
    """Two hackathons; unique searchable tokens per participant."""
    async with factory() as session:
        await seed_dev_users(session)
        now = utcnow()
        h1 = HackathonRow(
            id=uuid4(),
            title="H1",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        h2 = HackathonRow(
            id=uuid4(),
            title="H2",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        session.add_all([h1, h2])
        c1 = ChallengeRow(
            id=uuid4(),
            hackathon_id=h1.id,
            title="C1",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        c2 = ChallengeRow(
            id=uuid4(),
            hackathon_id=h2.id,
            title="C2",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
        )
        session.add_all([c1, c2])
        await session.flush()

        async def add_doc(chal_id, participant_id, token: str):
            sub = SubmissionRow(
                id=uuid4(),
                challenge_id=chal_id,
                participant_id=participant_id,
                status=SubmissionStatus.CREATED.value,
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(sub)
            await session.flush()
            job_id = uuid4()
            key = f"a/{token}.md"
            session.add(
                IngestionJobRow(
                    id=job_id,
                    submission_id=sub.id,
                    artifact_key=key,
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
            text = f"# Doc\n\nUnique token {token} for retrieval tenancy tests.\n"
            drafts = chunk_hybrid(normalize_to_canonical(text), max_chars=1200)
            await DocumentChunkRepository(session).replace_for_job(
                ingestion_job_id=job_id,
                submission_id=sub.id,
                artifact_key=key,
                parser_version=PARSER_VERSION,
                chunker_version=CHUNKER_VERSION,
                chunks=drafts,
            )
            return sub.id

        await add_doc(c1.id, PARTICIPANT_ID, "alphatenantone")
        await add_doc(c1.id, PARTICIPANT_2_ID, "betatenantpeer")
        await add_doc(c2.id, PARTICIPANT_ID, "gammacrosshack")
        await session.commit()
        return h1.id, h2.id, c1.id


@pytest.mark.asyncio
async def test_sql_filters_block_cross_hackathon_and_peer_chunks():
    engine, factory = await _prepare()
    try:
        h1, h2, _c1 = await _seed_two_tenants(factory)
        async with factory() as session:
            # Organizer of h1 sees alpha + beta, not gamma
            from challengeforge.retrieval.lexical import RetrievalFilters

            a = await retrieve(
                session,
                "alphatenantone",
                top_k=5,
                filters=RetrievalFilters(hackathon_id=h1),
            )
            b = await retrieve(
                session,
                "betatenantpeer",
                top_k=5,
                filters=RetrievalFilters(hackathon_id=h1),
            )
            g = await retrieve(
                session,
                "gammacrosshack",
                top_k=5,
                filters=RetrievalFilters(hackathon_id=h1),
            )
            assert len(a) == 1
            assert len(b) == 1
            assert len(g) == 0

            # Participant 1 only sees own chunk in h1
            own = await retrieve(
                session,
                "alphatenantone",
                top_k=5,
                filters=RetrievalFilters(
                    hackathon_id=h1, participant_id=PARTICIPANT_ID
                ),
            )
            peer = await retrieve(
                session,
                "betatenantpeer",
                top_k=5,
                filters=RetrievalFilters(
                    hackathon_id=h1, participant_id=PARTICIPANT_ID
                ),
            )
            assert len(own) == 1
            assert len(peer) == 0

            # Cross-hackathon empty for h2 organizer scope querying alpha
            cross = await retrieve(
                session,
                "alphatenantone",
                top_k=5,
                filters=RetrievalFilters(hackathon_id=h2),
            )
            assert len(cross) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_service_scopes_empty_for_participant_without_chunks():
    """Service-level search (avoids app fixture drop_all lock hangs on Windows)."""
    from challengeforge.application.search import SearchService
    from challengeforge.application import UnitOfWork
    from challengeforge.config import Settings
    from challengeforge.storage.filesystem import LocalFilesystemStorage
    from pathlib import Path

    engine, factory = await _prepare()
    try:
        async with factory() as session:
            await seed_dev_users(session)
            now = utcnow()
            hack = HackathonRow(
                id=uuid4(),
                title="Owned",
                description="d",
                status=HackathonStatus.PUBLISHED.value,
                organizer_id=ORGANIZER_ID,
                created_at=now,
                updated_at=now,
            )
            session.add(hack)
            await session.commit()
            hack_id = hack.id

        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(database_url="postgresql+asyncpg://x"),
            )
            svc = SearchService(uow)
            hits = await svc.search_hackathon(
                CurrentUser(
                    id=PARTICIPANT_ID, display_name="Pat", role=UserRole.PARTICIPANT
                ),
                hack_id,
                query="anything",
            )
            assert hits == []
            foreign = CurrentUser(
                id=uuid4(), display_name="OtherOrg", role=UserRole.ORGANIZER
            )
            with pytest.raises(PermissionDenied):
                await svc.search_hackathon(foreign, hack_id, query="anything")
    finally:
        await engine.dispose()
