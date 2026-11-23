"""Challenge leaderboard reads with publish-gate visibility."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from challengeforge.application import UnitOfWork
from challengeforge.domain.enums import HackathonStatus, UserRole
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied
from challengeforge.identity import CurrentUser


@dataclass(frozen=True)
class LeaderboardEntryView:
    rank: int
    submission_id: UUID
    participant_id: UUID
    score: int
    completed_at: datetime
    evaluation_id: UUID


@dataclass(frozen=True)
class LeaderboardView:
    challenge_id: UUID
    hackathon_id: UUID
    entries: list[LeaderboardEntryView]


class LeaderboardService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def get_challenge_leaderboard(
        self, actor: CurrentUser, challenge_id: UUID
    ) -> LeaderboardView:
        challenge = await self.uow.challenges.get(challenge_id)
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if hackathon is None:
            raise NotFoundError("Hackathon not found.")

        if actor.role == UserRole.ORGANIZER:
            if hackathon.organizer_id != actor.id:
                raise PermissionDenied(
                    "Only the owning organizer may view this leaderboard."
                )
        elif actor.role == UserRole.PARTICIPANT:
            if hackathon.status != HackathonStatus.CLOSED:
                raise PermissionDenied(
                    "Leaderboard is visible to participants only after the hackathon closes."
                )
        else:
            raise PermissionDenied("Unsupported role for leaderboard.")

        rows = await self.uow.leaderboards.list_ranked_for_challenge(challenge_id)
        return LeaderboardView(
            challenge_id=challenge_id,
            hackathon_id=hackathon.id,
            entries=[
                LeaderboardEntryView(
                    rank=r["rank"],
                    submission_id=r["submission_id"],
                    participant_id=r["participant_id"],
                    score=r["score"],
                    completed_at=r["completed_at"],
                    evaluation_id=r["evaluation_id"],
                )
                for r in rows
            ],
        )
