"""Bounded LIGHT bypass policy: narrow responsiveness without HEAVY starvation."""

from __future__ import annotations

from challengeforge.config import Settings
from challengeforge.persistence.repositories import EvaluationRepository
from challengeforge.persistence.session import get_session_factory
from tests.conftest import participant_headers, published_challenge


async def _enqueue(client, challenge_id: str, index: int, workload: str) -> str:
    created = await client.post(
        f"/api/v1/challenges/{challenge_id}/submissions",
        headers={
            **participant_headers(),
            "Idempotency-Key": f"sched-{workload}-{index}",
        },
        json={"metadata": {"workload_class": workload, "index": index}},
    )
    assert created.status_code == 201
    submitted = await client.post(
        f"/api/v1/submissions/{created.json()['id']}/submit",
        headers=participant_headers(),
    )
    assert submitted.status_code == 200
    return submitted.json()["evaluation_id"]


async def _claim(worker: str, **overrides):
    factory = get_session_factory()
    async with factory() as session:
        claimed = await EvaluationRepository(session).claim_next(
            worker_id=worker,
            max_workers=2,
            max_concurrent_heavy=1,
            light_bypass_limit=2,
            **overrides,
        )
        if claimed is None:
            await session.rollback()
        else:
            await session.commit()
        return claimed


async def _complete(evaluation_id, worker: str) -> None:
    factory = get_session_factory()
    async with factory() as session:
        result = await EvaluationRepository(session).mark_succeeded(
            evaluation_id,
            score=1,
            result_metadata={},
            worker_id=worker,
        )
        assert result is not None
        await session.commit()


async def test_pure_light_remains_fifo(client):
    challenge = await published_challenge(client)
    expected = [
        await _enqueue(client, challenge["id"], i, "light") for i in range(4)
    ]
    actual = []
    for i in range(4):
        claimed = await _claim(f"fifo-{i}")
        assert claimed is not None
        actual.append(str(claimed.id))
        await _complete(claimed.id, f"fifo-{i}")
    assert actual == expected


async def test_blocked_heavy_allows_only_two_consecutive_light_bypasses(client):
    challenge = await published_challenge(client)
    running_id = await _enqueue(client, challenge["id"], 0, "heavy")
    running = await _claim("heavy-running")
    assert str(running.id) == running_id

    blocked_heavy_id = await _enqueue(client, challenge["id"], 1, "heavy")
    light_ids = [
        await _enqueue(client, challenge["id"], i, "light") for i in range(2, 6)
    ]

    first = await _claim("light-1")
    assert str(first.id) == light_ids[0]
    await _complete(first.id, "light-1")
    second = await _claim("light-2")
    assert str(second.id) == light_ids[1]
    await _complete(second.id, "light-2")

    # Bound reached: capacity is deliberately left idle rather than starving HEAVY.
    assert await _claim("light-3") is None
    await _complete(running.id, "heavy-running")
    heavy = await _claim("heavy-next")
    assert str(heavy.id) == blocked_heavy_id


async def test_continuous_light_arrival_cannot_starve_heavy(client):
    challenge = await published_challenge(client)
    await _enqueue(client, challenge["id"], 10, "heavy")
    running = await _claim("active-heavy")
    blocked_id = await _enqueue(client, challenge["id"], 11, "heavy")

    for index in (12, 13):
        expected_light = await _enqueue(client, challenge["id"], index, "light")
        claimed = await _claim(f"continuous-{index}")
        assert str(claimed.id) == expected_light
        await _complete(claimed.id, f"continuous-{index}")

    for index in range(14, 19):
        await _enqueue(client, challenge["id"], index, "light")
        assert await _claim(f"cannot-starve-{index}") is None

    await _complete(running.id, "active-heavy")
    heavy = await _claim("starvation-prevented")
    assert str(heavy.id) == blocked_id


async def test_second_heavy_is_not_bypassed_to_reach_lights(client):
    challenge = await published_challenge(client)
    await _enqueue(client, challenge["id"], 20, "heavy")
    running = await _claim("first-heavy")
    oldest_blocked = await _enqueue(client, challenge["id"], 21, "heavy")
    await _enqueue(client, challenge["id"], 22, "heavy")
    for index in range(23, 26):
        await _enqueue(client, challenge["id"], index, "light")

    # Only an immediate LIGHT successor may bypass; LIGHT is not a priority lane.
    assert await _claim("no-arbitrary-priority") is None
    await _complete(running.id, "first-heavy")
    next_heavy = await _claim("second-heavy")
    assert str(next_heavy.id) == oldest_blocked


async def test_pure_fifo_policy_never_bypasses_blocked_heavy(client):
    challenge = await published_challenge(client)
    await _enqueue(client, challenge["id"], 30, "heavy")
    await _claim("fifo-active")
    await _enqueue(client, challenge["id"], 31, "heavy")
    await _enqueue(client, challenge["id"], 32, "light")
    assert (
        await _claim("fifo-policy", scheduling_policy="fifo")
    ) is None


async def test_default_interactive_budget_allows_only_one_running(client):
    challenge = await published_challenge(client)
    await _enqueue(client, challenge["id"], 40, "light")
    await _enqueue(client, challenge["id"], 41, "light")
    settings = Settings()
    assert settings.evaluation_max_workers == 1

    factory = get_session_factory()
    async with factory() as session:
        first = await EvaluationRepository(session).claim_next(
            worker_id="budget-first",
            max_workers=settings.evaluation_max_workers,
        )
        await session.commit()
    assert first is not None

    async with factory() as session:
        second = await EvaluationRepository(session).claim_next(
            worker_id="budget-second",
            max_workers=settings.evaluation_max_workers,
        )
        await session.rollback()
    assert second is None
