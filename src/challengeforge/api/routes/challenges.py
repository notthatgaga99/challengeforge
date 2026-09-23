from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, Response, UploadFile, status

from challengeforge.api.deps import (
    challenge_service,
    evaluation_service,
    get_app_settings,
    get_current_user,
    get_storage,
    submission_service,
)
from challengeforge.api.routes.hackathons import serialize_challenge
from challengeforge.api.schemas import (
    ChallengeResponse,
    EvaluationBacklogResponse,
    EvaluationResponse,
    OrganizerEvaluationQueueResponse,
    SubmissionCreateRequest,
    SubmissionResponse,
    SubmissionUpdateRequest,
)
from challengeforge.application.challenges import ChallengeService
from challengeforge.application.evaluations import (
    EvaluationBacklogStatus,
    EvaluationService,
    ParticipantEvaluationView,
)
from challengeforge.application.submissions import SubmissionService
from challengeforge.config import Settings
from challengeforge.domain.models import Evaluation, Submission
from challengeforge.identity import CurrentUser
from challengeforge.storage.base import ArtifactStorage

router = APIRouter()


def serialize_submission(
    item: Submission,
    *,
    replayed: bool | None = None,
    evaluation: Evaluation | None = None,
    backlog: EvaluationBacklogStatus | None = None,
) -> SubmissionResponse:
    return SubmissionResponse(
        id=item.id,
        challenge_id=item.challenge_id,
        participant_id=item.participant_id,
        status=item.status.value,
        metadata=item.metadata,
        artifact_key=item.artifact_key,
        created_at=item.created_at,
        updated_at=item.updated_at,
        failure_reason=item.failure_reason,
        idempotent_replay=replayed,
        evaluation_id=evaluation.id if evaluation else None,
        evaluation_status=evaluation.status.value if evaluation else None,
        evaluation_async=True if evaluation else None,
        evaluation_health=backlog.health.value if backlog else None,
        evaluation_message=(
            backlog.message
            if backlog
            else (
                "Your submission was accepted. Evaluation happens asynchronously."
                if evaluation
                else None
            )
        ),
        # Per-submission wait is intentionally unknown under heterogeneous,
        # bypassable work; do not reuse a whole-queue service-rate estimate.
        estimated_wait_seconds=None,
    )


def serialize_evaluation(view: ParticipantEvaluationView) -> EvaluationResponse:
    return EvaluationResponse(
        id=view.id,
        submission_id=view.submission_id,
        status=view.status.value,
        estimated_jobs_ahead=view.estimated_jobs_ahead,
        estimated_wait_seconds=view.estimated_wait_seconds,
        estimate_is_approximate=view.estimate_is_approximate,
        score=view.score,
        failure_message=view.failure_message,
        created_at=view.created_at,
        started_at=view.started_at,
        completed_at=view.completed_at,
        submission_accepted=view.submission_accepted,
        message=view.message,
    )


def serialize_backlog(item: EvaluationBacklogStatus) -> EvaluationBacklogResponse:
    return EvaluationBacklogResponse(
        health=item.health.value,
        queued_count=item.queued_count,
        running_count=item.running_count,
        succeeded_count=item.succeeded_count,
        failed_count=item.failed_count,
        oldest_queue_age_seconds=item.oldest_queue_age_seconds,
        estimated_wait_seconds=item.estimated_wait_seconds,
        recent_service_rate_per_second=item.recent_service_rate_per_second,
        message=item.message,
        submissions_accepted=item.submissions_accepted,
        estimate_is_approximate=item.estimate_is_approximate,
        pressure_state=item.pressure_state,
        adaptive_max_workers=item.adaptive_max_workers,
        resource_runtime_enabled=item.resource_runtime_enabled,
    )


def serialize_organizer_queue(
    item: EvaluationBacklogStatus,
) -> OrganizerEvaluationQueueResponse:
    base = serialize_backlog(item).model_dump()
    return OrganizerEvaluationQueueResponse(
        **base,
        light_queued_count=item.light_queued_count,
        medium_queued_count=item.medium_queued_count,
        heavy_queued_count=item.heavy_queued_count,
        worker_capacity=item.worker_capacity,
    )


@router.get("/challenges/{challenge_id}", response_model=ChallengeResponse)
async def get_challenge(
    challenge_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> ChallengeResponse:
    item = await service.get_challenge(actor, challenge_id)
    return serialize_challenge(item)


@router.post("/challenges/{challenge_id}/publish", response_model=ChallengeResponse)
async def publish_challenge(
    challenge_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> ChallengeResponse:
    item = await service.publish_challenge(actor, challenge_id)
    return serialize_challenge(item)


@router.post("/challenges/{challenge_id}/close", response_model=ChallengeResponse)
async def close_challenge(
    challenge_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> ChallengeResponse:
    item = await service.close_challenge(actor, challenge_id)
    return serialize_challenge(item)


@router.get("/challenges/{challenge_id}/submissions", response_model=list[SubmissionResponse])
async def list_challenge_submissions(
    challenge_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: ChallengeService = Depends(challenge_service),
) -> list[SubmissionResponse]:
    items = await service.list_submissions(actor, challenge_id)
    return [serialize_submission(item) for item in items]


@router.post(
    "/challenges/{challenge_id}/submissions",
    response_model=SubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_submission(
    challenge_id: UUID,
    payload: SubmissionCreateRequest,
    response: Response,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> SubmissionResponse:
    result = await service.create(
        actor,
        challenge_id,
        metadata=payload.metadata,
        idempotency_key=idempotency_key,
    )
    if result.replayed:
        response.status_code = status.HTTP_200_OK
    return serialize_submission(result.submission, replayed=result.replayed)


@router.get("/submissions/{submission_id}", response_model=SubmissionResponse)
async def get_submission(
    submission_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
) -> SubmissionResponse:
    item = await service.get(actor, submission_id)
    return serialize_submission(item)


@router.patch("/submissions/{submission_id}", response_model=SubmissionResponse)
async def update_submission(
    submission_id: UUID,
    payload: SubmissionUpdateRequest,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
) -> SubmissionResponse:
    item = await service.update_created(
        actor, submission_id, metadata=payload.metadata
    )
    return serialize_submission(item)


@router.post("/submissions/{submission_id}/artifact", response_model=SubmissionResponse)
async def attach_artifact(
    submission_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
    storage: ArtifactStorage = Depends(get_storage),
    settings: Settings = Depends(get_app_settings),
    file: UploadFile = File(...),
    content_sha256: str | None = Header(default=None, alias="X-Content-SHA256"),
) -> SubmissionResponse:
    """Stream upload to staging, finalize to final key, then commit metadata.

    Does not buffer the entire body in memory. Streaming ≠ resumability.
    """
    from challengeforge.application.uploads import (
        cleanup_incoming,
        iter_upload_file,
        stream_to_incoming,
    )

    incoming = None
    try:
        incoming = await stream_to_incoming(
            storage.incoming_root(),
            iter_upload_file(file),
            max_bytes=settings.artifact_max_bytes,
            content_type=file.content_type or "application/octet-stream",
            expected_sha256=content_sha256,
        )
        item = await service.attach_incoming(
            actor,
            submission_id,
            incoming,
            artifact_filename=file.filename,
        )
        return serialize_submission(item)
    except Exception:
        if incoming is not None:
            incoming.abort()
        raise
    finally:
        # Opportunistic: reclaim very old abandoned staging (best-effort).
        try:
            cleanup_incoming(storage.incoming_root(), grace_seconds=3600.0)
        except Exception:
            pass


@router.post("/submissions/{submission_id}/submit", response_model=SubmissionResponse)
async def submit_submission(
    submission_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
    eval_service: EvaluationService = Depends(evaluation_service),
) -> SubmissionResponse:
    result = await service.submit(actor, submission_id)
    backlog = await eval_service.get_backlog_status()
    return serialize_submission(
        result.submission, evaluation=result.evaluation, backlog=backlog
    )


@router.post("/submissions/{submission_id}/cancel", response_model=SubmissionResponse)
async def cancel_submission(
    submission_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
) -> SubmissionResponse:
    item = await service.cancel(actor, submission_id)
    return serialize_submission(item)


@router.get(
    "/submissions/{submission_id}/evaluation",
    response_model=EvaluationResponse,
)
async def get_submission_evaluation(
    submission_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: EvaluationService = Depends(evaluation_service),
) -> EvaluationResponse:
    view = await service.get_participant_view(actor, submission_id)
    return serialize_evaluation(view)


@router.get("/evaluations/backlog", response_model=EvaluationBacklogResponse)
async def get_evaluation_backlog(
    actor: CurrentUser = Depends(get_current_user),
    service: EvaluationService = Depends(evaluation_service),
) -> EvaluationBacklogResponse:
    """Approximate evaluation system health. Submissions remain accepted."""
    _ = actor
    return serialize_backlog(await service.get_backlog_status())


@router.get(
    "/organizer/evaluation-queue",
    response_model=OrganizerEvaluationQueueResponse,
)
async def get_organizer_evaluation_queue(
    actor: CurrentUser = Depends(get_current_user),
    service: EvaluationService = Depends(evaluation_service),
) -> OrganizerEvaluationQueueResponse:
    """Minimal organizer visibility into the evaluation backlog."""
    return serialize_organizer_queue(await service.get_organizer_queue(actor))


@router.get("/users/{user_id}/submissions", response_model=list[SubmissionResponse])
async def list_user_submissions(
    user_id: UUID,
    actor: CurrentUser = Depends(get_current_user),
    service: SubmissionService = Depends(submission_service),
) -> list[SubmissionResponse]:
    items = await service.list_for_user(actor, user_id)
    return [serialize_submission(item) for item in items]
