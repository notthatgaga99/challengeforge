"""Evaluation lifecycle: submit → QUEUED → claim → SUCCEEDED/FAILED + recovery."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from challengeforge.application.evaluator import EvaluationFailed, evaluate_submission
from challengeforge.config import Settings
from challengeforge.domain.enums import EvaluationStatus
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.models import EvaluationRow
from challengeforge.persistence.repositories import EvaluationRepository
from challengeforge.persistence.session import get_session_factory
from challengeforge.worker import EvaluationWorker
from tests.conftest import participant_headers, published_challenge


async def _create_and_submit(client, challenge_id: str, *, metadata: dict | None = None, key: str | None = None):
    headers = {**participant_headers()}
    if key:
        headers["Idempotency-Key"] = key
    created = await client.post(
        f"/api/v1/challenges/{challenge_id}/submissions",
        headers=headers,
        json={"metadata": metadata or {"note": "eval"}},
    )
    assert created.status_code in (200, 201)
    submission_id = created.json()["id"]
    submitted = await client.post(
        f"/api/v1/submissions/{submission_id}/submit",
        headers=participant_headers(),
    )
    assert submitted.status_code == 200
    body = submitted.json()
    assert body["status"] == "submitted"
    assert body["evaluation_async"] is True
    assert body["evaluation_status"] == "queued"
    assert body["evaluation_id"] is not None
    assert "score" not in body or body.get("score") is None
    return body


async def test_submit_creates_queued_evaluation(client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-normal")
    evaluation = await client.get(
        f"/api/v1/submissions/{body['id']}/evaluation",
        headers=participant_headers(),
    )
    assert evaluation.status_code == 200
    payload = evaluation.json()
    assert payload["status"] == "queued"
    assert payload["score"] is None
    assert payload["id"] == body["evaluation_id"]


async def test_worker_completes_evaluation(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-worker")
    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
        evaluation_poll_interval_seconds=0.05,
    )
    worker = EvaluationWorker(settings, worker_id="test-worker-a")
    await worker.drain(max_idle_rounds=2, timeout_seconds=10)
    assert worker.jobs_completed >= 1

    evaluation = await client.get(
        f"/api/v1/submissions/{body['id']}/evaluation",
        headers=participant_headers(),
    )
    payload = evaluation.json()
    assert payload["status"] == "succeeded"
    assert payload["score"] is not None
    assert 0 <= payload["score"] <= 100
    assert payload["completed_at"] is not None
    assert payload["submission_accepted"] is True
    assert "attempt_count" not in payload
    assert "worker_id" not in payload


async def test_two_workers_claim_one_evaluation_once(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-race")
    evaluation_id = body["evaluation_id"]
    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=20,
        evaluation_poll_interval_seconds=0.01,
    )
    w1 = EvaluationWorker(settings, worker_id="race-1")
    w2 = EvaluationWorker(settings, worker_id="race-2")
    await asyncio.gather(
        w1.drain(max_idle_rounds=3, timeout_seconds=15),
        w2.drain(max_idle_rounds=3, timeout_seconds=15),
    )
    assert w1.claims + w2.claims == 1
    assert w1.jobs_completed + w2.jobs_completed == 1

    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(EvaluationRow, __import__("uuid").UUID(evaluation_id))
        assert row is not None
        assert row.status == "succeeded"
        assert row.attempt_count == 1


async def test_evaluator_failure_is_terminal(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(
        client,
        challenge["id"],
        metadata={"force_evaluation_failure": True},
        key="eval-fail",
    )
    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_fake_work_ms=5,
    )
    worker = EvaluationWorker(settings, worker_id="fail-worker")
    await worker.drain(max_idle_rounds=2, timeout_seconds=10)
    assert worker.jobs_failed == 1

    evaluation = (
        await client.get(
            f"/api/v1/submissions/{body['id']}/evaluation",
            headers=participant_headers(),
        )
    ).json()
    assert evaluation["status"] == "failed"
    assert evaluation["score"] is None
    assert evaluation["submission_accepted"] is True
    assert evaluation["failure_message"]
    assert "submission remains" in evaluation["failure_message"].lower() or "could not complete" in evaluation["failure_message"].lower()


async def test_stale_running_requeued_then_completes(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-stale")
    evaluation_id = __import__("uuid").UUID(body["evaluation_id"])
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(EvaluationRow, evaluation_id)
        assert row is not None
        row.status = "running"
        row.started_at = utcnow() - timedelta(seconds=120)
        row.attempt_count = 1
        row.worker_id = "crashed-worker"
        await session.commit()

    settings = Settings(
        database_url=app.state.settings.database_url,
        artifact_root=app.state.settings.artifact_root,
        evaluation_stale_after_seconds=30,
        evaluation_max_attempts=3,
        evaluation_fake_work_ms=5,
    )
    worker = EvaluationWorker(settings, worker_id="recovery-worker")
    recovered = await worker.recover_stale()
    assert recovered == 1

    async with factory() as session:
        row = await session.get(EvaluationRow, evaluation_id)
        assert row.status == "queued"
        assert row.attempt_count == 1

    await worker.drain(max_idle_rounds=2, timeout_seconds=10)
    async with factory() as session:
        row = await session.get(EvaluationRow, evaluation_id)
        assert row.status == "succeeded"
        assert row.attempt_count == 2


async def test_stale_running_exhausted_attempts_fails(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-exhausted")
    evaluation_id = __import__("uuid").UUID(body["evaluation_id"])
    factory = get_session_factory()
    async with factory() as session:
        row = await session.get(EvaluationRow, evaluation_id)
        assert row is not None
        row.status = "running"
        row.started_at = utcnow() - timedelta(seconds=120)
        row.attempt_count = 3
        row.worker_id = "dead"
        await session.commit()

        recovered = await EvaluationRepository(session).recover_stale_running(
            stale_before=utcnow() - timedelta(seconds=30),
            max_attempts=3,
        )
        await session.commit()
        assert len(recovered) == 1
        assert recovered[0].status == EvaluationStatus.FAILED

        row = await session.get(EvaluationRow, evaluation_id)
        assert row.status == "failed"
        assert "Abandoned after 3" in row.failure_reason


async def test_one_evaluation_per_submission_unique(app, client):
    challenge = await published_challenge(client)
    body = await _create_and_submit(client, challenge["id"], key="eval-unique")
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            EvaluationRow(
                id=uuid4(),
                submission_id=__import__("uuid").UUID(body["id"]),
                status="queued",
                workload_class="light",
                created_at=utcnow(),
                attempt_count=0,
                result_metadata={},
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_idempotent_create_then_single_submit_evaluation(client):
    challenge = await published_challenge(client)
    key = "eval-idem-key"
    first = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": key},
        json={"metadata": {"repo": "a"}},
    )
    second = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={**participant_headers(), "Idempotency-Key": key},
        json={"metadata": {"repo": "a"}},
    )
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    submitted = await client.post(
        f"/api/v1/submissions/{first.json()['id']}/submit",
        headers=participant_headers(),
    )
    replay_submit = await client.post(
        f"/api/v1/submissions/{first.json()['id']}/submit",
        headers=participant_headers(),
    )
    assert submitted.status_code == 200
    assert replay_submit.status_code == 200
    assert submitted.json()["evaluation_id"] == replay_submit.json()["evaluation_id"]

    factory = get_session_factory()
    async with factory() as session:
        counts = await EvaluationRepository(session).count_by_status()
        assert sum(counts.values()) == 1


async def test_deterministic_evaluator_score():
    sid = uuid4()
    a = evaluate_submission(submission_id=sid, metadata={"x": 1}, artifact_key=None)
    b = evaluate_submission(submission_id=sid, metadata={"x": 1}, artifact_key=None)
    assert a.score == b.score
    with pytest.raises(EvaluationFailed):
        evaluate_submission(
            submission_id=sid,
            metadata={"force_evaluation_failure": True},
            artifact_key=None,
        )
