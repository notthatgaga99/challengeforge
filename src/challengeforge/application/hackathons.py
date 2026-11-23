from uuid import UUID

from challengeforge.application import UnitOfWork, require_non_empty, require_organizer
from challengeforge.domain.enums import HackathonStatus
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied, ValidationFailed
from challengeforge.domain.models import Hackathon
from challengeforge.identity import CurrentUser


class HackathonService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def list_hackathons(self, actor: CurrentUser) -> list[Hackathon]:
        return await self.uow.hackathons.list_visible(include_unpublished=actor.is_organizer)

    async def get_hackathon(self, actor: CurrentUser, hackathon_id: UUID) -> Hackathon:
        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")
        if actor.is_organizer:
            return hackathon
        if hackathon.status not in (
            HackathonStatus.PUBLISHED,
            HackathonStatus.CLOSED,
        ):
            raise NotFoundError("Hackathon not found.")
        return hackathon

    async def create_hackathon(
        self, actor: CurrentUser, title: str, description: str
    ) -> Hackathon:
        require_organizer(actor)
        title = require_non_empty(title, "title")
        description = require_non_empty(description, "description")
        hackathon = await self.uow.hackathons.add(
            title=title, description=description, organizer_id=actor.id
        )
        await self.uow.session.commit()
        return hackathon

    async def publish_hackathon(self, actor: CurrentUser, hackathon_id: UUID) -> Hackathon:
        require_organizer(actor)
        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")
        if hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may publish this hackathon.")
        if hackathon.status == HackathonStatus.PUBLISHED:
            return hackathon
        if hackathon.status != HackathonStatus.DRAFT:
            raise ValidationFailed("Only draft hackathons can be published.")
        published = await self.uow.hackathons.save_status(
            hackathon_id, HackathonStatus.PUBLISHED
        )
        await self.uow.session.commit()
        return published

    async def close_hackathon(self, actor: CurrentUser, hackathon_id: UUID) -> Hackathon:
        """Close publishes participant-visible leaderboards (ADR 0016)."""
        require_organizer(actor)
        hackathon = await self.uow.hackathons.get(hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")
        if hackathon.organizer_id != actor.id:
            raise PermissionDenied("Only the creating organizer may close this hackathon.")
        if hackathon.status == HackathonStatus.CLOSED:
            return hackathon
        if hackathon.status != HackathonStatus.PUBLISHED:
            raise ValidationFailed("Only published hackathons can be closed.")
        closed = await self.uow.hackathons.save_status(
            hackathon_id, HackathonStatus.CLOSED
        )
        from challengeforge.outbox.events import (
            EVENT_HACKATHON_CLOSED,
            hackathon_closed_key,
        )
        from challengeforge.persistence.repositories import OutboxRepository

        await OutboxRepository(self.uow.session).enqueue(
            event_type=EVENT_HACKATHON_CLOSED,
            aggregate_type="hackathon",
            aggregate_id=hackathon_id,
            payload={
                "hackathon_id": str(hackathon_id),
                "organizer_id": str(closed.organizer_id),
                "status": HackathonStatus.CLOSED.value,
            },
            idempotency_key=hackathon_closed_key(hackathon_id),
        )
        await self.uow.session.commit()
        return closed
