from collections.abc import AsyncIterator

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.application import UnitOfWork
from challengeforge.application.challenges import ChallengeService
from challengeforge.application.evaluations import EvaluationService
from challengeforge.application.hackathons import HackathonService
from challengeforge.application.submissions import SubmissionService
from challengeforge.config import Settings
from challengeforge.domain.exceptions import Unauthenticated
from challengeforge.identity import CurrentUser
from challengeforge.persistence.repositories import UserRepository
from challengeforge.persistence.session import get_session_factory
from challengeforge.storage.base import ArtifactStorage
from challengeforge.storage.filesystem import LocalFilesystemStorage

USER_HEADER = APIKeyHeader(name="X-User-Id", auto_error=False)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_storage(settings: Settings = Depends(get_app_settings)) -> ArtifactStorage:
    return LocalFilesystemStorage(settings.artifact_root)


def get_uow(
    session: AsyncSession = Depends(get_db_session),
    storage: ArtifactStorage = Depends(get_storage),
    settings: Settings = Depends(get_app_settings),
) -> UnitOfWork:
    return UnitOfWork(session=session, storage=storage, settings=settings)


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    x_user_id: str | None = Depends(USER_HEADER),
) -> CurrentUser:
    raw = x_user_id or request.cookies.get("cf_user_id")
    if not raw:
        raise Unauthenticated("Missing development identity. Send X-User-Id.")
    try:
        from uuid import UUID

        user_id = UUID(raw)
    except ValueError as exc:
        raise Unauthenticated("Invalid X-User-Id.") from exc
    user = await UserRepository(session).get(user_id)
    if user is None:
        raise Unauthenticated("Unknown development user.")
    return CurrentUser.from_user(user)


def hackathon_service(uow: UnitOfWork = Depends(get_uow)) -> HackathonService:
    return HackathonService(uow)


def challenge_service(uow: UnitOfWork = Depends(get_uow)) -> ChallengeService:
    return ChallengeService(uow)


def submission_service(uow: UnitOfWork = Depends(get_uow)) -> SubmissionService:
    return SubmissionService(uow)


def evaluation_service(uow: UnitOfWork = Depends(get_uow)) -> EvaluationService:
    return EvaluationService(uow)
