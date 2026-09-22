from uuid import UUID

from challengeforge.application import UnitOfWork, require_non_empty, require_organizer
from challengeforge.domain.enums import ChallengeStatus, HackathonStatus
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied, ValidationFailed
from challengeforge.domain.models import Challenge, Submission
from challengeforge.identity import CurrentUser


class ChallengeService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def list_for_hackathon(
        self, actor: CurrentUser, hackathon_id: UUID
    ) -> list[Challenge]:
        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")
        if hackathon.status != HackathonStatus.PUBLISHED and not actor.is_organizer:
            raise NotFoundError("Hackathon not found.")
        return await self.uow.challenges.list_for_hackathon(
            hackathon_id, include_unpublished=actor.is_organizer
        )

    async def get_challenge(self, actor: CurrentUser, challenge_id: UUID) -> Challenge:
        challenge = await self.uow.challenges.get(challenge_id)
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        visible_to_participant = (
            hackathon is not None
            and hackathon.status == HackathonStatus.PUBLISHED
            and challenge.status == ChallengeStatus.PUBLISHED
        )
        if not visible_to_participant and not actor.is_organizer:
            raise NotFoundError("Challenge not found.")
        return challenge

    async def create_challenge(
        self,
        actor: CurrentUser,
        hackathon_id: UUID,
        *,
        title: str,
        description: str,
        constraints: str,
        specification_body: str,
        criteria: list[tuple[str, str, int]],
    ) -> Challenge:
        require_organizer(actor)
        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")
        if hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may add challenges.")
        title = require_non_empty(title, "title")
        description = require_non_empty(description, "description")
        specification_body = require_non_empty(specification_body, "specification")
        parsed: list[tuple[str, str, int]] = []
        for name, criterion_description, weight in criteria:
            if weight < 1:
                raise ValidationFailed("Evaluation criterion weight must be >= 1.")
            parsed.append(
                (
                    require_non_empty(name, "criterion name"),
                    require_non_empty(criterion_description, "criterion description"),
                    weight,
                )
            )
        challenge = await self.uow.challenges.add(
            hackathon_id=hackathon_id,
            title=title,
            description=description,
            constraints=constraints or "",
            specification_body=specification_body,
            criteria=parsed,
        )
        await self.uow.session.commit()
        return challenge

    async def publish_challenge(self, actor: CurrentUser, challenge_id: UUID) -> Challenge:
        require_organizer(actor)
        challenge = await self.uow.challenges.get(challenge_id)
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if hackathon is None or hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may publish this challenge.")
        if challenge.status == ChallengeStatus.PUBLISHED:
            return challenge
        if challenge.status != ChallengeStatus.DRAFT:
            raise ValidationFailed("Only draft challenges can be published.")
        if not challenge.is_publishable():
            raise ValidationFailed(
                "A challenge needs a specification and at least one evaluation criterion before publish."
            )
        published = await self.uow.challenges.save_status(
            challenge_id, ChallengeStatus.PUBLISHED
        )
        await self.uow.session.commit()
        return published

    async def close_challenge(
        self, actor: CurrentUser, challenge_id: UUID
    ) -> Challenge:
        require_organizer(actor)
        # Submission acceptance takes FOR SHARE on this row. This FOR UPDATE
        # establishes whether acceptance or close is authoritative first.
        challenge = await self.uow.challenges.get_for_close(challenge_id)
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if hackathon is None or hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may close this challenge.")
        if challenge.status == ChallengeStatus.CLOSED:
            return challenge
        if challenge.status != ChallengeStatus.PUBLISHED:
            raise ValidationFailed("Only published challenges can be closed.")
        closed = await self.uow.challenges.save_status(
            challenge_id, ChallengeStatus.CLOSED
        )
        await self.uow.session.commit()
        return closed

    async def list_submissions(
        self, actor: CurrentUser, challenge_id: UUID
    ) -> list[Submission]:
        require_organizer(actor)
        challenge = await self.uow.challenges.get(challenge_id)
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if hackathon is None or hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may list these submissions.")
        return await self.uow.submissions.list_for_challenge(challenge_id)
