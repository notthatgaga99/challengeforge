import asyncio
from uuid import UUID

import pytest

from challengeforge.domain.enums import ChallengeStatus, SubmissionStatus
from challengeforge.identity import PARTICIPANT_ID
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.repositories import (
    ChallengeRepository,
    SubmissionRepository,
)
from challengeforge.persistence.session import get_session_factory
from tests.conftest import participant_headers, published_challenge


@pytest.mark.parametrize(
    ("winner", "loser"),
    [
        (SubmissionStatus.SUBMITTED, SubmissionStatus.CANCELLED),
        (SubmissionStatus.CANCELLED, SubmissionStatus.SUBMITTED),
    ],
)
async def test_conditional_terminal_transition_has_one_winner(
    app, client, winner: SubmissionStatus, loser: SubmissionStatus
):
    challenge = await published_challenge(client)
    created = await client.post(
        f"/api/v1/challenges/{challenge['id']}/submissions",
        headers={
            **participant_headers(),
            "Idempotency-Key": f"transition-{winner.value}",
        },
        json={"metadata": {"winner": winner.value}},
    )
    submission_id = UUID(created.json()["id"])
    factory = get_session_factory()

    async with factory() as winner_session, factory() as loser_session:
        winning = await SubmissionRepository(winner_session).transition_if_created(
            submission_id=submission_id,
            participant_id=PARTICIPANT_ID,
            target_status=winner.value,
            updated_at=utcnow(),
        )
        assert winning is not None

        losing_task = asyncio.create_task(
            SubmissionRepository(loser_session).transition_if_created(
                submission_id=submission_id,
                participant_id=PARTICIPANT_ID,
                target_status=loser.value,
                updated_at=utcnow(),
            )
        )
        await asyncio.sleep(0.05)
        assert not losing_task.done(), "loser should wait on the winning row update"

        await winner_session.commit()
        losing = await asyncio.wait_for(losing_task, timeout=2)
        assert losing is None
        await loser_session.rollback()

    final = await client.get(
        f"/api/v1/submissions/{submission_id}",
        headers=participant_headers(),
    )
    assert final.json()["status"] == winner.value

    action = "cancel" if loser == SubmissionStatus.CANCELLED else "submit"
    losing_response = await client.post(
        f"/api/v1/submissions/{submission_id}/{action}",
        headers=participant_headers(),
    )
    assert losing_response.status_code == 409
    assert losing_response.json()["error"]["code"] == "invalid_state_transition"


async def test_submission_gate_wins_before_close(app, client):
    challenge = await published_challenge(client)
    challenge_id = UUID(challenge["id"])
    factory = get_session_factory()

    async with factory() as acceptance_session, factory() as close_session:
        locked = await ChallengeRepository(
            acceptance_session
        ).get_for_submission_acceptance(challenge_id)
        assert locked is not None
        assert locked.status == ChallengeStatus.PUBLISHED

        async def close() -> None:
            close_repo = ChallengeRepository(close_session)
            to_close = await close_repo.get_for_close(challenge_id)
            assert to_close is not None
            await close_repo.save_status(challenge_id, ChallengeStatus.CLOSED)
            await close_session.commit()

        close_task = asyncio.create_task(close())
        await asyncio.sleep(0.05)
        assert not close_task.done(), "close should wait for accepted submission"

        await SubmissionRepository(acceptance_session).add(
            challenge_id=challenge_id,
            participant_id=PARTICIPANT_ID,
            metadata={"ordering": "acceptance-first"},
            artifact_key=None,
            idempotency_key="acceptance-first",
            request_fingerprint="acceptance-first-fingerprint",
        )
        await acceptance_session.commit()
        await asyncio.wait_for(close_task, timeout=2)

    async with factory() as verify_session:
        final_challenge = await ChallengeRepository(verify_session).get(challenge_id)
    assert final_challenge is not None
    assert final_challenge.status == ChallengeStatus.CLOSED


async def test_close_gate_wins_before_submission_acceptance(app, client):
    challenge = await published_challenge(client)
    challenge_id = UUID(challenge["id"])
    factory = get_session_factory()

    async with factory() as close_session, factory() as acceptance_session:
        close_repo = ChallengeRepository(close_session)
        locked = await close_repo.get_for_close(challenge_id)
        assert locked is not None
        await close_repo.save_status(challenge_id, ChallengeStatus.CLOSED)

        acceptance_task = asyncio.create_task(
            ChallengeRepository(
                acceptance_session
            ).get_for_submission_acceptance(challenge_id)
        )
        await asyncio.sleep(0.05)
        assert not acceptance_task.done(), "acceptance should wait for close"

        await close_session.commit()
        observed = await asyncio.wait_for(acceptance_task, timeout=2)
        assert observed is not None
        assert observed.status == ChallengeStatus.CLOSED
        await acceptance_session.rollback()
