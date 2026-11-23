"""Postgres fixed-window abuse budgets (ADR 0020)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from challengeforge.api.errors import register_error_handlers
from challengeforge.application import UnitOfWork
from challengeforge.application.rate_limits import (
    ACTION_SEARCH,
    ACTION_SUBMISSION_CREATE,
    RateLimitService,
)
from challengeforge.application.search import SearchService
from challengeforge.application.submissions import SubmissionService
from challengeforge.config import Settings
from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    UserRole,
)
from challengeforge.domain.exceptions import RateLimited
from challengeforge.identity import (
    ORGANIZER_ID,
    PARTICIPANT_ID,
    CurrentUser,
)
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import ChallengeRow, HackathonRow
from challengeforge.persistence.repositories import RateLimitRepository
from challengeforge.persistence.seed import seed_dev_users
from challengeforge.storage.filesystem import LocalFilesystemStorage
from tests.test_semantic_retrieval import _prepare


def _participant() -> CurrentUser:
    return CurrentUser(
        id=PARTICIPANT_ID, display_name="Pat", role=UserRole.PARTICIPANT
    )


async def _seed_published_challenge(factory):
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
        return hack.id, chal.id


def _uow(session, settings: Settings) -> UnitOfWork:
    return UnitOfWork(
        session=session,
        storage=LocalFilesystemStorage(Path(".")),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_fixed_window_denies_over_limit():
    engine, factory = await _prepare()
    try:
        async with factory() as session:
            await seed_dev_users(session)
            await session.commit()

        settings = Settings(
            database_url="postgresql+asyncpg://x",
            rate_limit_enabled=True,
            rate_limit_window_seconds=60,
            rate_limit_search=2,
        )
        actor = _participant()
        async with factory() as session:
            svc = RateLimitService(_uow(session, settings))
            await svc.check(actor, ACTION_SEARCH)
            await svc.check(actor, ACTION_SEARCH)
            with pytest.raises(RateLimited) as excinfo:
                await svc.check(actor, ACTION_SEARCH)
            assert excinfo.value.retry_after_seconds >= 1
            assert excinfo.value.http_status == 429

        async with factory() as session:
            started = RateLimitRepository.window_start(utcnow(), 60)
            count = await RateLimitRepository(session).get_count(
                subject_id=PARTICIPANT_ID,
                action=ACTION_SEARCH,
                window_started_at=started,
            )
            assert count == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_per_actor_isolation():
    engine, factory = await _prepare()
    try:
        async with factory() as session:
            await seed_dev_users(session)
            await session.commit()

        settings = Settings(
            database_url="postgresql+asyncpg://x",
            rate_limit_enabled=True,
            rate_limit_window_seconds=60,
            rate_limit_search=1,
        )
        a = _participant()
        b = CurrentUser(
            id=ORGANIZER_ID, display_name="Org", role=UserRole.ORGANIZER
        )
        async with factory() as session:
            svc = RateLimitService(_uow(session, settings))
            await svc.check(a, ACTION_SEARCH)
            with pytest.raises(RateLimited):
                await svc.check(a, ACTION_SEARCH)
            # Other actor still has budget.
            await svc.check(b, ACTION_SEARCH)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_kill_switch():
    engine, factory = await _prepare()
    try:
        settings = Settings(
            database_url="postgresql+asyncpg://x",
            rate_limit_enabled=False,
            rate_limit_search=1,
        )
        async with factory() as session:
            svc = RateLimitService(_uow(session, settings))
            for _ in range(5):
                await svc.check(_participant(), ACTION_SEARCH)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_create_replay_skips_quota():
    engine, factory = await _prepare()
    try:
        _, chal_id = await _seed_published_challenge(factory)
        settings = Settings(
            database_url="postgresql+asyncpg://x",
            rate_limit_enabled=True,
            rate_limit_window_seconds=60,
            rate_limit_submission_create=1,
            ingestion_enabled=False,
        )
        actor = _participant()
        key = "idem-rate-limit-1"
        async with factory() as session:
            svc = SubmissionService(_uow(session, settings))
            first = await svc.create(
                actor, chal_id, metadata={}, idempotency_key=key
            )
            # Replay must not consume the sole remaining unit / raise.
            replay = await svc.create(
                actor, chal_id, metadata={}, idempotency_key=key
            )
            assert replay.submission.id == first.submission.id
            assert replay.replayed is True

        async with factory() as session:
            started = RateLimitRepository.window_start(utcnow(), 60)
            count = await RateLimitRepository(session).get_count(
                subject_id=PARTICIPANT_ID,
                action=ACTION_SUBMISSION_CREATE,
                window_started_at=started,
            )
            assert count == 1

        async with factory() as session:
            svc = SubmissionService(_uow(session, settings))
            with pytest.raises(RateLimited):
                await svc.create(
                    actor, chal_id, metadata={}, idempotency_key="other-key"
                )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_search_service_enforces_budget():
    engine, factory = await _prepare()
    try:
        hack_id, _ = await _seed_published_challenge(factory)
        settings = Settings(
            database_url="postgresql+asyncpg://x",
            rate_limit_enabled=True,
            rate_limit_window_seconds=60,
            rate_limit_search=1,
            retrieval_default_version="cf-lex-fts-1",
        )
        actor = CurrentUser(
            id=ORGANIZER_ID, display_name="Org", role=UserRole.ORGANIZER
        )
        async with factory() as session:
            svc = SearchService(_uow(session, settings))
            await svc.search_hackathon(
                actor, hack_id, query="anything", retrieval_version="cf-lex-fts-1"
            )
            with pytest.raises(RateLimited):
                await svc.search_hackathon(
                    actor, hack_id, query="again", retrieval_version="cf-lex-fts-1"
                )
    finally:
        await engine.dispose()


def test_rate_limited_http_includes_retry_after():
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/boom")
    async def boom():
        raise RateLimited("too many", retry_after_seconds=42)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 429
    assert resp.headers["retry-after"] == "42"
    body = resp.json()
    assert body["error"]["code"] == "rate_limited"
    assert "too many" in body["error"]["message"]
