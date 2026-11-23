"""Leaderboard snapshot consistency + visibility."""

from __future__ import annotations

from uuid import uuid4

import pytest

from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
)
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_2_ID, PARTICIPANT_ID
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    ChallengeRow,
    EvaluationRow,
    HackathonRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import EvaluationRepository, LeaderboardRepository
from challengeforge.persistence.seed import seed_dev_users
from tests.test_semantic_retrieval import _prepare


@pytest.mark.asyncio
async def test_mark_succeeded_upserts_leaderboard_and_ranks():
    engine, factory = await _prepare()
    try:
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

            async def add_running(participant_id, worker="w1"):
                sub = SubmissionRow(
                    id=uuid4(),
                    challenge_id=chal.id,
                    participant_id=participant_id,
                    status=SubmissionStatus.SUBMITTED.value,
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(sub)
                await session.flush()
                ev = EvaluationRow(
                    id=uuid4(),
                    submission_id=sub.id,
                    status="running",
                    workload_class="light",
                    current_stage=0,
                    evaluation_mode="legacy",
                    created_at=now,
                    started_at=now,
                    attempt_count=1,
                    worker_id=worker,
                    result_metadata={},
                )
                session.add(ev)
                await session.flush()
                return sub.id, ev.id

            sub_a, ev_a = await add_running(PARTICIPANT_ID)
            sub_b, ev_b = await add_running(PARTICIPANT_2_ID)
            await session.commit()

        async with factory() as session:
            repo = EvaluationRepository(session)
            # Higher score first
            assert await repo.mark_succeeded(
                ev_a, score=90, result_metadata={}, worker_id="w1"
            )
            assert await repo.mark_succeeded(
                ev_b, score=80, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        async with factory() as session:
            ranked = await LeaderboardRepository(session).list_ranked_for_challenge(
                chal.id
            )
            assert len(ranked) == 2
            assert ranked[0]["submission_id"] == sub_a
            assert ranked[0]["rank"] == 1
            assert ranked[0]["score"] == 90
            assert ranked[1]["submission_id"] == sub_b
            assert ranked[1]["rank"] == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_tie_break_earlier_completion_wins_rank_share():
    engine, factory = await _prepare()
    try:
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
            subs = []
            for pid in (PARTICIPANT_ID, PARTICIPANT_2_ID):
                sub = SubmissionRow(
                    id=uuid4(),
                    challenge_id=chal.id,
                    participant_id=pid,
                    status=SubmissionStatus.SUBMITTED.value,
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
                session.add(sub)
                await session.flush()
                ev = EvaluationRow(
                    id=uuid4(),
                    submission_id=sub.id,
                    status="running",
                    workload_class="light",
                    current_stage=0,
                    evaluation_mode="legacy",
                    created_at=now,
                    started_at=now,
                    attempt_count=1,
                    worker_id="w1",
                    result_metadata={},
                )
                session.add(ev)
                await session.flush()
                subs.append((sub.id, ev.id))
            await session.commit()

        async with factory() as session:
            repo = EvaluationRepository(session)
            await repo.mark_succeeded(
                subs[0][1], score=70, result_metadata={}, worker_id="w1"
            )
            await repo.mark_succeeded(
                subs[1][1], score=70, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        async with factory() as session:
            ranked = await LeaderboardRepository(session).list_ranked_for_challenge(
                chal.id
            )
            assert ranked[0]["score"] == ranked[1]["score"] == 70
            # RANK() shares rank on ties
            assert ranked[0]["rank"] == ranked[1]["rank"] == 1
            # Earlier completed_at sorts first within same score
            assert ranked[0]["completed_at"] <= ranked[1]["completed_at"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_leaderboard_visibility_service():
    from pathlib import Path

    from challengeforge.application import UnitOfWork
    from challengeforge.application.leaderboards import LeaderboardService
    from challengeforge.config import Settings
    from challengeforge.domain.exceptions import PermissionDenied
    from challengeforge.domain.enums import UserRole
    from challengeforge.identity import CurrentUser
    from challengeforge.storage.filesystem import LocalFilesystemStorage

    engine, factory = await _prepare()
    try:
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
            await session.commit()
            chal_id = chal.id
            hack_id = hack.id

        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(database_url="postgresql+asyncpg://x"),
            )
            svc = LeaderboardService(uow)
            org = CurrentUser(
                id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER
            )
            view = await svc.get_challenge_leaderboard(org, chal_id)
            assert view.entries == []
            assert view.hackathon_id == hack_id

            pat = CurrentUser(
                id=PARTICIPANT_ID, display_name="Pat", role=UserRole.PARTICIPANT
            )
            with pytest.raises(PermissionDenied):
                await svc.get_challenge_leaderboard(pat, chal_id)

            await uow.hackathons.save_status(hack_id, HackathonStatus.CLOSED)
            await session.commit()

        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(database_url="postgresql+asyncpg://x"),
            )
            svc = LeaderboardService(uow)
            view = await svc.get_challenge_leaderboard(pat, chal_id)
            assert view.entries == []
    finally:
        await engine.dispose()
