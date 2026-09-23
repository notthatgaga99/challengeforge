from datetime import datetime, timezone
from uuid import UUID

from challengeforge.domain.enums import (
    ChallengeStatus,
    EvaluationStatus,
    HackathonStatus,
    IngestionStatus,
    SubmissionStatus,
    UserRole,
    WorkloadClass,
)
from challengeforge.domain.models import (
    Challenge,
    ChallengeSpecification,
    Evaluation,
    EvaluationCriterion,
    Hackathon,
    IngestionJob,
    Submission,
    User,
)
from challengeforge.persistence.models import (
    ChallengeRow,
    EvaluationRow,
    HackathonRow,
    IngestionJobRow,
    SubmissionRow,
    UserRow,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def user_to_domain(row: UserRow) -> User:
    return User(
        id=row.id,
        display_name=row.display_name,
        role=UserRole(row.role),
        created_at=row.created_at,
    )


def hackathon_to_domain(row: HackathonRow) -> Hackathon:
    return Hackathon(
        id=row.id,
        title=row.title,
        description=row.description,
        status=HackathonStatus(row.status),
        organizer_id=row.organizer_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def challenge_to_domain(row: ChallengeRow) -> Challenge:
    spec = row.specification
    return Challenge(
        id=row.id,
        hackathon_id=row.hackathon_id,
        title=row.title,
        description=row.description,
        constraints=row.constraints,
        status=ChallengeStatus(row.status),
        specification=ChallengeSpecification(
            body=spec.body if spec else "",
            created_at=spec.created_at if spec else row.created_at,
            updated_at=spec.updated_at if spec else row.updated_at,
        ),
        evaluation_criteria=[
            EvaluationCriterion(
                id=c.id,
                name=c.name,
                description=c.description,
                weight=c.weight,
            )
            for c in sorted(row.evaluation_criteria, key=lambda item: item.name)
        ],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def submission_to_domain(row: SubmissionRow) -> Submission:
    return Submission(
        id=row.id,
        challenge_id=row.challenge_id,
        participant_id=row.participant_id,
        status=SubmissionStatus(row.status),
        metadata=dict(row.metadata_json or {}),
        artifact_key=row.artifact_key,
        idempotency_key=row.idempotency_key,
        request_fingerprint=row.request_fingerprint,
        created_at=row.created_at,
        updated_at=row.updated_at,
        failure_reason=row.failure_reason,
    )


def evaluation_to_domain(row: EvaluationRow) -> Evaluation:
    return Evaluation(
        id=row.id,
        submission_id=row.submission_id,
        status=EvaluationStatus(row.status),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        attempt_count=row.attempt_count,
        failure_reason=row.failure_reason,
        score=row.score,
        result_metadata=dict(row.result_metadata or {}),
        worker_id=row.worker_id,
        workload_class=WorkloadClass(row.workload_class or WorkloadClass.LIGHT.value),
        current_stage=int(getattr(row, "current_stage", 0) or 0),
        evaluation_mode=str(getattr(row, "evaluation_mode", None) or "legacy"),
        deadline_at=getattr(row, "deadline_at", None),
    )


def ingestion_to_domain(row: IngestionJobRow) -> IngestionJob:
    return IngestionJob(
        id=row.id,
        submission_id=row.submission_id,
        artifact_key=row.artifact_key,
        status=IngestionStatus(row.status),
        attempt_count=row.attempt_count,
        available_at=row.available_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        worker_id=row.worker_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
        error_code=row.error_code,
        error_message=row.error_message,
        result_key=row.result_key,
    )


def new_user_row(user_id: UUID, display_name: str, role: UserRole) -> UserRow:
    return UserRow(
        id=user_id,
        display_name=display_name,
        role=role.value,
        created_at=utcnow(),
    )
