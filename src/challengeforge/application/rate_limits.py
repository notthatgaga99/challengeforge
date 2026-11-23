"""Abuse-budget checks (ADR 0020)."""

from __future__ import annotations

from challengeforge.application import UnitOfWork
from challengeforge.domain.exceptions import RateLimited
from challengeforge.identity import CurrentUser
from challengeforge.persistence.repositories import RateLimitRepository, utcnow

ACTION_SUBMISSION_CREATE = "submission_create"
ACTION_ARTIFACT_UPLOAD = "artifact_upload"
ACTION_SUBMISSION_SUBMIT = "submission_submit"
ACTION_SEARCH = "search"


class RateLimitService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def _limit_for(self, action: str) -> int:
        s = self.uow.settings
        return {
            ACTION_SUBMISSION_CREATE: s.rate_limit_submission_create,
            ACTION_ARTIFACT_UPLOAD: s.rate_limit_artifact_upload,
            ACTION_SUBMISSION_SUBMIT: s.rate_limit_submission_submit,
            ACTION_SEARCH: s.rate_limit_search,
        }[action]

    async def check(self, actor: CurrentUser, action: str) -> None:
        """Consume one unit; raise RateLimited if over budget."""
        if not self.uow.settings.rate_limit_enabled:
            return
        limit = self._limit_for(action)
        window = self.uow.settings.rate_limit_window_seconds
        now = utcnow()
        started = RateLimitRepository.window_start(now, window)
        count = await RateLimitRepository(self.uow.session).increment(
            subject_id=actor.id,
            action=action,
            window_started_at=started,
        )
        # Always commit the counter in its own TX so read-only paths (search)
        # and denied requests still persist consumption.
        await self.uow.session.commit()
        if count > limit:
            retry_after = max(
                1, int((started.timestamp() + window) - now.timestamp())
            )
            raise RateLimited(
                f"Rate limit exceeded for action '{action}' "
                f"({limit} per {window}s).",
                retry_after_seconds=retry_after,
            )
