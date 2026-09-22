"""Evaluation backlog health and participant-facing evaluation views."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from challengeforge.application import UnitOfWork, require_organizer
from challengeforge.domain.enums import EvaluationStatus
from challengeforge.domain.exceptions import NotFoundError, PermissionDenied
from challengeforge.domain.models import Evaluation, Submission
from challengeforge.identity import CurrentUser


class EvaluationHealth(StrEnum):
    NORMAL = "normal"
    BUSY = "busy"
    SATURATED = "saturated"
    CRITICAL = "critical"


@dataclass(frozen=True)
class EvaluationBacklogStatus:
    health: EvaluationHealth
    queued_count: int
    running_count: int
    succeeded_count: int
    failed_count: int
    oldest_queue_age_seconds: float | None
    estimated_wait_seconds: float | None
    recent_service_rate_per_second: float | None
    message: str
    submissions_accepted: bool
    estimate_is_approximate: bool = True
    light_queued_count: int = 0
    medium_queued_count: int = 0
    heavy_queued_count: int = 0
    worker_capacity: int = 0
    pressure_state: str = "normal"
    adaptive_max_workers: int | None = None
    resource_runtime_enabled: bool = True


@dataclass(frozen=True)
class ParticipantEvaluationView:
    """Product-facing evaluation state — no worker/lock internals."""

    id: UUID
    submission_id: UUID
    status: EvaluationStatus
    estimated_jobs_ahead: int | None
    estimated_wait_seconds: float | None
    estimate_is_approximate: bool
    score: int | None
    failure_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    submission_accepted: bool
    message: str


class EvaluationService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    async def get_for_submission(
        self, actor: CurrentUser, submission_id: UUID
    ) -> Evaluation:
        submission = await self.uow.submissions.get(submission_id)
        if submission is None:
            raise NotFoundError("Submission not found.")
        await self._authorize_view(actor, submission)
        evaluation = await self.uow.evaluations.get_by_submission(submission_id)
        if evaluation is None:
            raise NotFoundError("Evaluation not found for this submission.")
        return evaluation

    async def get_participant_view(
        self, actor: CurrentUser, submission_id: UUID
    ) -> ParticipantEvaluationView:
        evaluation = await self.get_for_submission(actor, submission_id)
        return await self._to_participant_view(evaluation)

    async def _to_participant_view(
        self, evaluation: Evaluation
    ) -> ParticipantEvaluationView:
        estimated_jobs_ahead = None
        estimated_wait = None
        if evaluation.status == EvaluationStatus.QUEUED:
            estimated_jobs_ahead = await self.uow.evaluations.queue_position(
                evaluation.id
            )
            # A single global service-rate estimate is false precision once
            # heterogeneous jobs can bypass one another. Return null until a
            # simple, defensible class-aware estimate exists.

        failure_message = None
        if evaluation.status == EvaluationStatus.FAILED:
            failure_message = self._friendly_failure(evaluation.failure_reason)

        messages = {
            EvaluationStatus.QUEUED: (
                "Your submission was accepted. Evaluation is waiting in the queue."
            ),
            EvaluationStatus.RUNNING: (
                "Your submission was accepted. Evaluation is in progress."
            ),
            EvaluationStatus.SUCCEEDED: (
                "Your submission was accepted and evaluation completed."
            ),
            EvaluationStatus.FAILED: (
                "Your submission was accepted, but evaluation could not complete."
            ),
        }
        return ParticipantEvaluationView(
            id=evaluation.id,
            submission_id=evaluation.submission_id,
            status=evaluation.status,
            estimated_jobs_ahead=estimated_jobs_ahead,
            estimated_wait_seconds=estimated_wait,
            estimate_is_approximate=True,
            score=(
                evaluation.score
                if evaluation.status == EvaluationStatus.SUCCEEDED
                else None
            ),
            failure_message=failure_message,
            created_at=evaluation.created_at,
            started_at=(
                evaluation.started_at
                if evaluation.status
                in (EvaluationStatus.RUNNING, EvaluationStatus.SUCCEEDED, EvaluationStatus.FAILED)
                else None
            ),
            completed_at=(
                evaluation.completed_at
                if evaluation.status
                in (EvaluationStatus.SUCCEEDED, EvaluationStatus.FAILED)
                else None
            ),
            submission_accepted=True,
            message=messages[evaluation.status],
        )

    async def _estimate_wait_for_position(
        self, position: int | None
    ) -> float | None:
        """Informational only. Returns null when throughput is unknown."""
        if position is None or position < 1:
            return None
        settings = self.uow.settings
        snap = await self.uow.evaluations.queue_snapshot(
            service_rate_window_seconds=settings.evaluation_service_rate_window_seconds
        )
        window = settings.evaluation_service_rate_window_seconds
        recent = snap["recent_completed_count"]
        if window <= 0 or recent < 3:
            # Need a few recent completions before inventing a wait number.
            return None
        service_rate = recent / window
        if service_rate <= 0:
            return None
        return round(position / service_rate, 1)

    @staticmethod
    def _friendly_failure(reason: str | None) -> str:
        if not reason:
            return "Evaluation could not complete. Your submission remains on record."
        if "Forced evaluation failure" in reason:
            return (
                "Evaluation could not complete for this submission. "
                "Your submission remains on record."
            )
        if "Abandoned after" in reason or "stale" in reason.lower():
            return (
                "Evaluation could not finish after several attempts. "
                "Your submission remains on record."
            )
        return (
            "Evaluation could not complete. Your submission remains on record."
        )

    async def get_backlog_status(self) -> EvaluationBacklogStatus:
        """Evaluation-system health. Never blocks submission acceptance."""
        settings = self.uow.settings
        snap = await self.uow.evaluations.queue_snapshot(
            service_rate_window_seconds=settings.evaluation_service_rate_window_seconds
        )
        queued = snap["queued"]
        oldest = snap["oldest_queue_age_seconds"]
        window = settings.evaluation_service_rate_window_seconds
        recent = snap["recent_completed_count"]
        service_rate = (recent / window) if window > 0 else None

        estimated_wait = None
        if queued > 0 and service_rate and service_rate > 0 and recent >= 3:
            estimated_wait = round(queued / service_rate, 1)

        health = EvaluationHealth.NORMAL
        if queued >= settings.evaluation_critical_queue_depth or (
            oldest is not None
            and oldest >= settings.evaluation_saturated_oldest_age_seconds * 5
        ):
            health = EvaluationHealth.CRITICAL
        elif queued >= settings.evaluation_saturated_queue_depth or (
            oldest is not None
            and oldest >= settings.evaluation_saturated_oldest_age_seconds
        ):
            health = EvaluationHealth.SATURATED
        elif queued >= settings.evaluation_busy_queue_depth or (
            oldest is not None and oldest >= settings.evaluation_busy_oldest_age_seconds
        ):
            health = EvaluationHealth.BUSY

        messages = {
            EvaluationHealth.NORMAL: (
                "Evaluation capacity looks healthy. Your score should appear shortly "
                "after submit."
            ),
            EvaluationHealth.BUSY: (
                "Your submission was accepted. Evaluation is delayed — the queue is busy."
            ),
            EvaluationHealth.SATURATED: (
                "Your submission was accepted and queued. Evaluation is significantly "
                "delayed."
            ),
            EvaluationHealth.CRITICAL: (
                "Your submission was accepted. Evaluation backlog is very large; "
                "scoring may take a long time, but your submission is durable."
            ),
        }
        runtime = await self.uow.evaluations.read_runtime_state()
        return EvaluationBacklogStatus(
            health=health,
            queued_count=queued,
            running_count=snap["running"],
            succeeded_count=snap["succeeded"],
            failed_count=snap["failed"],
            oldest_queue_age_seconds=(
                round(oldest, 3) if oldest is not None else None
            ),
            estimated_wait_seconds=estimated_wait,
            recent_service_rate_per_second=(
                round(service_rate, 3) if service_rate is not None else None
            ),
            message=messages[health],
            submissions_accepted=True,
            light_queued_count=snap["light_queued"],
            medium_queued_count=snap["medium_queued"],
            heavy_queued_count=snap["heavy_queued"],
            worker_capacity=settings.evaluation_max_workers,
            pressure_state=str(runtime.get("pressure_state") or "normal"),
            adaptive_max_workers=runtime.get("adaptive_max_workers"),
            resource_runtime_enabled=settings.resource_aware_runtime_enabled,
        )

    async def get_organizer_queue(self, actor: CurrentUser) -> EvaluationBacklogStatus:
        require_organizer(actor)
        return await self.get_backlog_status()

    async def _authorize_view(self, actor: CurrentUser, submission: Submission) -> None:
        if submission.participant_id == actor.id:
            return
        if not actor.is_organizer:
            raise PermissionDenied("You cannot view this evaluation.")
        challenge = await self.uow.challenges.get(submission.challenge_id)
        if challenge is None:
            raise NotFoundError("Submission not found.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if hackathon is None or hackathon.organizer_id != actor.id:
            raise PermissionDenied("You cannot view this evaluation.")
