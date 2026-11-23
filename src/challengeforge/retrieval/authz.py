"""Deny-by-default retrieval scope for the product search path."""

from __future__ import annotations

from uuid import UUID

from challengeforge.domain.enums import HackathonStatus, UserRole
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied
from challengeforge.domain.models import Hackathon
from challengeforge.identity import CurrentUser
from challengeforge.retrieval.lexical import RetrievalFilters


def resolve_retrieval_scope(
    actor: CurrentUser,
    hackathon: Hackathon,
    *,
    challenge_id: UUID | None = None,
    challenge_hackathon_id: UUID | None = None,
) -> RetrievalFilters:
    """Build effective SQL filters. Client cannot widen beyond this result.

    When ``challenge_id`` is set, ``challenge_hackathon_id`` must equal
    ``hackathon.id`` (caller loads the challenge and passes its hackathon_id).
    """
    if challenge_id is not None:
        if challenge_hackathon_id is None:
            raise PermissionDenied("Challenge ownership could not be verified.")
        if challenge_hackathon_id != hackathon.id:
            raise PermissionDenied("Challenge is not in this hackathon.")

    if actor.role == UserRole.ORGANIZER:
        if hackathon.organizer_id != actor.id:
            raise PermissionDenied(
                "Only the owning organizer may search this hackathon."
            )
        return RetrievalFilters(
            hackathon_id=hackathon.id,
            challenge_id=challenge_id,
        )

    if actor.role == UserRole.PARTICIPANT:
        if hackathon.status not in (HackathonStatus.PUBLISHED, HackathonStatus.CLOSED):
            raise NotFoundError("Hackathon not found.")
        return RetrievalFilters(
            hackathon_id=hackathon.id,
            challenge_id=challenge_id,
            participant_id=actor.id,
        )

    raise PermissionDenied("Unsupported role for search.")
