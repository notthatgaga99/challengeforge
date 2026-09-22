"""FIFO claim ordering and participant evaluation journey tests."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from challengeforge.config import Settings
from challengeforge.domain.enums import EvaluationStatus
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import EvaluationRow
from challengeforge.persistence.repositories import EvaluationRepository
from challengeforge.persistence.session import get_session_factory
from challengeforge.worker import EvaluationWorker
from tests.conftest import (
    organizer_headers,
    participant_headers,
    published_challenge,
)


async def _submit(client, challenge_id: str, *, key: str, metadata: dict | None = None, headers=None):
    hdrs = {**(headers or participant_headers()), "Idempotency-Key": key}
    created = await client.post(
        f"/api/v1/challenges/{challenge_id}/submissions",
        headers=hdrs,
        json={"metadata": metadata or {"k": key}},
    )
    assert created.status_code in (200, 201)
    submitted = await client.post(
        f"/api/v1/submissions/{created.json()['id']}/submit",
        headers={**(headers or participant_headers())},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "submitted"
    assert submitted.json()["evaluation_async"] is True
    return submitted.json()


async def test_fifo_claim_order_simple(app, client):
    challenge = await published_challenge(client)
    bodies = []
    for i in range(5):
        bodies.append(await _submit(client, challenge["id"], key=f"fifo-a-{i}"))
        await asyncio.sleep(0.02)

    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
        evaluation_poll_interval_seconds=0.01,
    )
    worker = EvaluationWorker(settings, worker_id="fifo-single")
    claim_order: list[str] = []
    for _ in range(5):
        claimed = None
        factory = get_session_factory()
        async with factory() as session:
            claimed = await EvaluationRepository(session).claim_next(worker_id="fifo-single")
            await session.commit()
        assert claimed is not None
        claim_order.append(str(claimed.submission_id))
        # Complete so we don't leave RUNNING for other tests
        async with factory() as session:
            await EvaluationRepository(session).mark_succeeded(
                claimed.id,
                score=1,
                result_metadata={},
                worker_id="fifo-single",
            )
            await session.commit()

    expected = [b["id"] for b in bodies]
    assert claim_order == expected


async def test_fifo_tie_break_by_id(app):
    factory = get_session_factory()
    from challengeforge.domain.enums import ChallengeStatus, HackathonStatus
    from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
    from challengeforge.persistence.models import (
        ChallengeRow,
        ChallengeSpecificationRow,
        HackathonRow,
        SubmissionRow,
    )

    now = utcnow()
    tie_time = now - timedelta(seconds=10)
    async with factory() as session:
        hackathon = HackathonRow(
            id=uuid4(),
            title="FIFO tie",
            description="d",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        challenge = ChallengeRow(
            id=uuid4(),
            hackathon_id=hackathon.id,
            title="C",
            description="d",
            constraints="",
            status=ChallengeStatus.PUBLISHED.value,
            created_at=now,
            updated_at=now,
            specification=ChallengeSpecificationRow(
                body="spec", created_at=now, updated_at=now
            ),
        )
        session.add_all([hackathon, challenge])
        await session.flush()
        ids = sorted([uuid4(), uuid4(), uuid4()])
        for eid, sid in zip(ids, [uuid4(), uuid4(), uuid4()]):
            session.add(
                SubmissionRow(
                    id=sid,
                    challenge_id=challenge.id,
                    participant_id=PARTICIPANT_ID,
                    status="submitted",
                    metadata_json={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                EvaluationRow(
                    id=eid,
                    submission_id=sid,
                    status="queued",
                    workload_class="light",
                    created_at=tie_time,
                    attempt_count=0,
                    result_metadata={},
                )
            )
        await session.commit()

        claim_ids = []
        for _ in range(3):
            claimed = await EvaluationRepository(session).claim_next(worker_id="tie")
            assert claimed is not None
            claim_ids.append(claimed.id)
            await EvaluationRepository(session).mark_succeeded(
                claimed.id, score=1, result_metadata={}, worker_id="tie"
            )
        await session.commit()
        assert claim_ids == ids


async def test_fifo_multi_worker_unique_claims(app, client):
    challenge = await published_challenge(client)
    for i in range(8):
        await _submit(client, challenge["id"], key=f"fifo-mw-{i}")

    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=15,
        evaluation_poll_interval_seconds=0.01,
    )
    workers = [
        EvaluationWorker(settings, worker_id=f"mw-{i}") for i in range(3)
    ]
    await asyncio.gather(
        *[w.drain(max_idle_rounds=4, timeout_seconds=30) for w in workers]
    )
    assert sum(w.claims for w in workers) == 8
    assert sum(w.jobs_completed for w in workers) == 8

    factory = get_session_factory()
    async with factory() as session:
        counts = await EvaluationRepository(session).count_by_status()
        assert counts.get("succeeded", 0) == 8
        assert counts.get("queued", 0) == 0


async def test_fifo_failed_does_not_block_later(app, client):
    challenge = await published_challenge(client)
    await _submit(
        client,
        challenge["id"],
        key="fifo-fail-first",
        metadata={"force_evaluation_failure": True},
    )
    second = await _submit(client, challenge["id"], key="fifo-fail-second")

    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
    )
    worker = EvaluationWorker(settings, worker_id="fifo-fail")
    await worker.drain(max_idle_rounds=3, timeout_seconds=20)
    assert worker.jobs_failed == 1
    assert worker.jobs_completed == 1

    ev = await client.get(
        f"/api/v1/submissions/{second['id']}/evaluation",
        headers=participant_headers(),
    )
    assert ev.json()["status"] == "succeeded"


async def test_journey_submit_to_score(app, client):
    challenge = await published_challenge(client)
    body = await _submit(client, challenge["id"], key="journey-a")
    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
    )
    await EvaluationWorker(settings, worker_id="j-a").drain(timeout_seconds=15)
    ev = (
        await client.get(
            f"/api/v1/submissions/{body['id']}/evaluation",
            headers=participant_headers(),
        )
    ).json()
    assert ev["status"] == "succeeded"
    assert ev["score"] is not None
    assert ev["submission_accepted"] is True


async def test_journey_refresh_same_ids(client):
    challenge = await published_challenge(client)
    body = await _submit(client, challenge["id"], key="journey-b")
    first = await client.get(
        f"/api/v1/submissions/{body['id']}/evaluation",
        headers=participant_headers(),
    )
    second = await client.get(
        f"/api/v1/submissions/{body['id']}/evaluation",
        headers=participant_headers(),
    )
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["submission_id"] == body["id"]
    assert first.json()["status"] == "queued"
    assert first.json()["estimated_jobs_ahead"] >= 0
    assert "position" not in first.json()


async def test_journey_idempotent_submit_same_evaluation(client):
    challenge = await published_challenge(client)
    key = "journey-c-idem"
    first = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": key},
        json={"metadata": {"same": True}},
    )
    replay = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": key},
        json={"metadata": {"same": True}},
    )
    assert first.json()["id"] == replay.json()["id"]
    s1 = await client.post(
        f"/api/v1/submissions/{first.json()['id']}/submit",
        headers=participant_headers(),
    )
    s2 = await client.post(
        f"/api/v1/submissions/{first.json()['id']}/submit",
        headers=participant_headers(),
    )
    assert s1.json()["evaluation_id"] == s2.json()["evaluation_id"]


async def test_journey_queue_positions_fifo(client):
    challenge = await published_challenge(client)
    bodies = []
    for i in range(10):
        bodies.append(await _submit(client, challenge["id"], key=f"journey-d-{i}"))
        await asyncio.sleep(0.01)

    positions = []
    for body in bodies:
        ev = (
            await client.get(
                f"/api/v1/submissions/{body['id']}/evaluation",
                headers=participant_headers(),
            )
        ).json()
        positions.append(ev["estimated_jobs_ahead"])
        assert ev["estimated_wait_seconds"] is None
    assert positions == list(range(10))


async def test_journey_worker_crash_recovery_visible(app, client):
    challenge = await published_challenge(client)
    body = await _submit(client, challenge["id"], key="journey-e")
    evaluation_id = UUID(body["evaluation_id"])
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(EvaluationRow, evaluation_id)
        row.status = "running"
        row.started_at = utcnow() - timedelta(seconds=120)
        row.attempt_count = 1
        row.worker_id = "crashed"
        await session.commit()

    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_stale_after_seconds=30,
        evaluation_max_attempts=3,
        evaluation_fake_work_ms=5,
    )
    worker = EvaluationWorker(settings, worker_id="journey-e-healer")
    await worker.recover_stale()
    await worker.drain(timeout_seconds=15)
    ev = (
        await client.get(
            f"/api/v1/submissions/{body['id']}/evaluation",
            headers=participant_headers(),
        )
    ).json()
    assert ev["status"] == "succeeded"
    assert ev["id"] == str(evaluation_id)


async def test_journey_eval_failed_submission_still_accepted(app, client):
    challenge = await published_challenge(client)
    body = await _submit(
        client,
        challenge["id"],
        key="journey-f",
        metadata={"force_evaluation_failure": True},
    )
    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
    )
    await EvaluationWorker(settings, worker_id="j-f").drain(timeout_seconds=15)

    sub = await client.get(
        f"/api/v1/submissions/{body['id']}", headers=participant_headers()
    )
    ev = await client.get(
        f"/api/v1/submissions/{body['id']}/evaluation",
        headers=participant_headers(),
    )
    assert sub.json()["status"] == "submitted"
    assert ev.json()["status"] == "failed"
    assert ev.json()["submission_accepted"] is True
    assert ev.json()["score"] is None


async def test_organizer_queue_endpoint(client):
    challenge = await published_challenge(client)
    await _submit(client, challenge["id"], key="org-q-1")
    denied = await client.get(
        "/api/v1/organizer/evaluation-queue", headers=participant_headers()
    )
    assert denied.status_code == 403
    ok = await client.get(
        "/api/v1/organizer/evaluation-queue", headers=organizer_headers()
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["queued_count"] >= 1
    assert "oldest_queue_age_seconds" in body
    assert body["light_queued_count"] >= 1
    assert body["medium_queued_count"] == 0
    assert body["heavy_queued_count"] == 0
    assert body["worker_capacity"] == 1


async def test_submission_page_renders(client):
    challenge = await published_challenge(client)
    body = await _submit(client, challenge["id"], key="ui-page")
    page = await client.get(f"/submissions/{body['id']}")
    assert page.status_code == 200
    assert "evaluation-panel" in page.text
    assert "POLL_MS" in page.text


async def test_organizer_page_has_queue_card(client):
    page = await client.get("/organizer")
    assert page.status_code == 200
    assert "eval-queue" in page.text
    assert "organizer/evaluation-queue" in page.text
