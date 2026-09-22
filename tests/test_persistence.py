from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from challengeforge.domain.enums import ChallengeStatus, HackathonStatus
from challengeforge.identity import ORGANIZER_ID, PARTICIPANT_ID
from challengeforge.persistence.models import (
    ChallengeRow,
    ChallengeSpecificationRow,
    HackathonRow,
    SubmissionRow,
)
from challengeforge.persistence.session import get_session_factory


def _now():
    return datetime.now(timezone.utc)


async def test_submission_requires_existing_challenge(app):
    factory = get_session_factory()
    async with factory() as session:
        session.add(
            SubmissionRow(
                id=uuid4(),
                challenge_id=uuid4(),
                participant_id=PARTICIPANT_ID,
                status="created",
                metadata_json={},
                created_at=_now(),
                updated_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_idempotency_unique_per_participant(app):
    factory = get_session_factory()
    async with factory() as session:
        now = _now()
        hackathon = HackathonRow(
            id=uuid4(),
            title="H",
            description="D",
            status=HackathonStatus.PUBLISHED.value,
            organizer_id=ORGANIZER_ID,
            created_at=now,
            updated_at=now,
        )
        challenge = ChallengeRow(
            id=uuid4(),
            hackathon_id=hackathon.id,
            title="C",
            description="D",
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
        session.add(
            SubmissionRow(
                id=uuid4(),
                challenge_id=challenge.id,
                participant_id=PARTICIPANT_ID,
                status="created",
                metadata_json={},
                idempotency_key="dup",
                request_fingerprint="fingerprint-a",
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        session.add(
            SubmissionRow(
                id=uuid4(),
                challenge_id=challenge.id,
                participant_id=PARTICIPANT_ID,
                status="created",
                metadata_json={},
                idempotency_key="dup",
                request_fingerprint="fingerprint-a",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_user_role_constraint(app):
    from challengeforge.persistence.models import UserRow

    factory = get_session_factory()
    async with factory() as session:
        session.add(
            UserRow(
                id=uuid4(),
                display_name="Bad",
                role="admin",
                created_at=_now(),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
