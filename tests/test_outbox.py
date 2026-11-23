"""Transactional outbox — same-TX enqueue, relay, idempotency, rollback safety."""

from __future__ import annotations

from uuid import uuid4

import pytest

from challengeforge.domain.enums import (
    ChallengeStatus,
    HackathonStatus,
    SubmissionStatus,
    UserRole,
)
from challengeforge.identity import (
    ORGANIZER_ID,
    PARTICIPANT_ID,
    CurrentUser,
)
from challengeforge.outbox.events import (
    EVENT_EVALUATION_SUCCEEDED,
    EVENT_HACKATHON_CLOSED,
    evaluation_succeeded_key,
    hackathon_closed_key,
)
from challengeforge.outbox.relay import OutboxRelay
from challengeforge.outbox.sinks import RecordingNotificationSink
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import (
    ChallengeRow,
    EvaluationRow,
    HackathonRow,
    SubmissionRow,
)
from challengeforge.persistence.repositories import (
    EvaluationRepository,
    OutboxRepository,
)
from challengeforge.persistence.seed import seed_dev_users
from tests.test_semantic_retrieval import _prepare


async def _seed_running_evaluation(factory):
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
        await session.commit()
        return hack.id, chal.id, sub.id, ev.id


@pytest.mark.asyncio
async def test_mark_succeeded_enqueues_outbox_in_same_tx():
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            result = await EvaluationRepository(session).mark_succeeded(
                ev_id, score=88, result_metadata={}, worker_id="w1"
            )
            assert result is not None
            pending = await OutboxRepository(session).count_pending()
            assert pending == 1
            event = await OutboxRepository(session).get_by_idempotency_key(
                evaluation_succeeded_key(ev_id)
            )
            assert event is not None
            assert event.event_type == EVENT_EVALUATION_SUCCEEDED
            assert event.published_at is None
            assert event.payload["score"] == 88
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_rollback_discards_outbox_and_evaluation():
    """Business write + outbox share fate — dual-write avoided by construction."""
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                ev_id, score=50, result_metadata={}, worker_id="w1"
            )
            assert await OutboxRepository(session).count_pending() == 1
            await session.rollback()

        async with factory() as session:
            assert await OutboxRepository(session).count_pending() == 0
            assert (
                await OutboxRepository(session).get_by_idempotency_key(
                    evaluation_succeeded_key(ev_id)
                )
                is None
            )
            from sqlalchemy import select

            row = (
                await session.execute(
                    select(EvaluationRow).where(EvaluationRow.id == ev_id)
                )
            ).scalar_one()
            assert row.status == "running"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_idempotent_enqueue_on_duplicate_key():
    engine, factory = await _prepare()
    try:
        async with factory() as session:
            await seed_dev_users(session)
            repo = OutboxRepository(session)
            a = await repo.enqueue(
                event_type=EVENT_HACKATHON_CLOSED,
                aggregate_type="hackathon",
                aggregate_id=uuid4(),
                payload={"n": 1},
                idempotency_key="hackathon.closed:once",
            )
            b = await repo.enqueue(
                event_type=EVENT_HACKATHON_CLOSED,
                aggregate_type="hackathon",
                aggregate_id=a.aggregate_id,
                payload={"n": 2},
                idempotency_key="hackathon.closed:once",
            )
            await session.commit()
            assert a.id == b.id
            assert await repo.count_pending() == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relay_publishes_and_is_idempotent_at_sink():
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                ev_id, score=91, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        sink = RecordingNotificationSink()
        relay = OutboxRelay(factory, sink=sink, batch_size=10)
        n = await relay.drain_once()
        assert n == 1
        assert len(sink.delivered) == 1
        assert sink.delivered[0].event_type == EVENT_EVALUATION_SUCCEEDED
        assert sink.delivered[0].idempotency_key == evaluation_succeeded_key(ev_id)

        async with factory() as session:
            assert await OutboxRepository(session).count_pending() == 0
            event = await OutboxRepository(session).get_by_idempotency_key(
                evaluation_succeeded_key(ev_id)
            )
            assert event is not None
            assert event.published_at is not None

        # Second drain is empty; re-deliver same event would be sink no-op.
        assert await relay.drain_once() == 0
        assert len(sink.delivered) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_relay_retries_after_sink_failure():
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                ev_id, score=70, result_metadata={}, worker_id="w1"
            )
            await session.commit()

        sink = RecordingNotificationSink(fail_times=1)
        relay = OutboxRelay(factory, sink=sink)
        assert await relay.drain_once() == 0
        assert relay.failed == 1
        async with factory() as session:
            assert await OutboxRepository(session).count_pending() == 1
            event = await OutboxRepository(session).get_by_idempotency_key(
                evaluation_succeeded_key(ev_id)
            )
            assert event is not None
            assert event.attempt_count == 1
            assert event.last_error is not None

        assert await relay.drain_once() == 1
        assert len(sink.delivered) == 1
        async with factory() as session:
            assert await OutboxRepository(session).count_pending() == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_close_hackathon_enqueues_and_relays():
    from pathlib import Path

    from challengeforge.application import UnitOfWork
    from challengeforge.application.hackathons import HackathonService
    from challengeforge.config import Settings
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
            await session.commit()
            hack_id = hack.id

        async with factory() as session:
            uow = UnitOfWork(
                session=session,
                storage=LocalFilesystemStorage(Path(".")),
                settings=Settings(),
            )
            svc = HackathonService(uow)
            closed = await svc.close_hackathon(
                CurrentUser(
                    id=ORGANIZER_ID, display_name="Ada", role=UserRole.ORGANIZER
                ),
                hack_id,
            )
            assert closed.status == HackathonStatus.CLOSED

        async with factory() as session:
            event = await OutboxRepository(session).get_by_idempotency_key(
                hackathon_closed_key(hack_id)
            )
            assert event is not None
            assert event.event_type == EVENT_HACKATHON_CLOSED
            assert event.published_at is None

        sink = RecordingNotificationSink()
        assert await OutboxRelay(factory, sink=sink).drain_all() == 1
        assert sink.delivered[0].event_type == EVENT_HACKATHON_CLOSED
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_stale_worker_succeed_skips_outbox():
    """mark_succeeded no-ops when not RUNNING for worker — no phantom event."""
    engine, factory = await _prepare()
    try:
        _h, _c, _s, ev_id = await _seed_running_evaluation(factory)
        async with factory() as session:
            result = await EvaluationRepository(session).mark_succeeded(
                ev_id, score=10, result_metadata={}, worker_id="other-worker"
            )
            assert result is None
            assert await OutboxRepository(session).count_pending() == 0
            await session.commit()
    finally:
        await engine.dispose()
