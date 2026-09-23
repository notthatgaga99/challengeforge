from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.config import Settings
from challengeforge.domain.exceptions import PermissionDenied, ValidationFailed
from challengeforge.identity import CurrentUser
from challengeforge.persistence.repositories import (
    ChallengeRepository,
    EvaluationRepository,
    HackathonRepository,
    IngestionJobRepository,
    SubmissionRepository,
    UserRepository,
)
from challengeforge.storage.base import ArtifactStorage


@dataclass
class UnitOfWork:
    session: AsyncSession
    storage: ArtifactStorage
    settings: Settings

    @property
    def users(self) -> UserRepository:
        return UserRepository(self.session)

    @property
    def hackathons(self) -> HackathonRepository:
        return HackathonRepository(self.session)

    @property
    def challenges(self) -> ChallengeRepository:
        return ChallengeRepository(self.session)

    @property
    def submissions(self) -> SubmissionRepository:
        return SubmissionRepository(self.session)

    @property
    def evaluations(self) -> EvaluationRepository:
        return EvaluationRepository(self.session)

    @property
    def ingestion_jobs(self) -> IngestionJobRepository:
        return IngestionJobRepository(self.session)


def require_organizer(user: CurrentUser) -> None:
    if not user.is_organizer:
        raise PermissionDenied("Only organizers may perform this action.")


def require_participant(user: CurrentUser) -> None:
    if not user.is_participant:
        raise PermissionDenied("Only participants may perform this action.")


def require_non_empty(value: str, field: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValidationFailed(f"{field} must not be empty.")
    return cleaned
