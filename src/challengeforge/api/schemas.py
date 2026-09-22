from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class HackathonCreateRequest(BaseModel):
    title: str
    description: str


class HackathonResponse(BaseModel):
    id: UUID
    title: str
    description: str
    status: str
    organizer_id: UUID
    created_at: datetime
    updated_at: datetime


class EvaluationCriterionRequest(BaseModel):
    name: str
    description: str
    weight: int = Field(default=1, ge=1)


class ChallengeSpecificationRequest(BaseModel):
    body: str


class ChallengeCreateRequest(BaseModel):
    title: str
    description: str
    constraints: str = ""
    specification: ChallengeSpecificationRequest
    evaluation_criteria: list[EvaluationCriterionRequest] = Field(default_factory=list)


class EvaluationCriterionResponse(BaseModel):
    id: UUID
    name: str
    description: str
    weight: int


class ChallengeSpecificationResponse(BaseModel):
    body: str
    created_at: datetime
    updated_at: datetime


class ChallengeResponse(BaseModel):
    id: UUID
    hackathon_id: UUID
    title: str
    description: str
    constraints: str
    status: str
    specification: ChallengeSpecificationResponse
    evaluation_criteria: list[EvaluationCriterionResponse]
    created_at: datetime
    updated_at: datetime


class SubmissionCreateRequest(BaseModel):
    metadata: dict[str, Any] = Field(default_factory=dict)


class SubmissionUpdateRequest(BaseModel):
    metadata: dict[str, Any]


class SubmissionResponse(BaseModel):
    id: UUID
    challenge_id: UUID
    participant_id: UUID
    status: str
    metadata: dict[str, Any]
    artifact_key: str | None
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None
    idempotent_replay: bool | None = None
    # Present on successful submit: evaluation is async; score is not yet known.
    evaluation_id: UUID | None = None
    evaluation_status: str | None = None
    evaluation_async: bool | None = None
    evaluation_health: str | None = None
    evaluation_message: str | None = None
    estimated_wait_seconds: float | None = None


class EvaluationResponse(BaseModel):
    """Participant-facing evaluation state."""

    id: UUID
    submission_id: UUID
    status: str
    estimated_jobs_ahead: int | None = None
    estimated_wait_seconds: float | None = None
    estimate_is_approximate: bool = True
    score: int | None = None
    failure_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    submission_accepted: bool = True
    message: str = ""


class EvaluationBacklogResponse(BaseModel):
    health: str
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


class OrganizerEvaluationQueueResponse(EvaluationBacklogResponse):
    light_queued_count: int
    medium_queued_count: int
    heavy_queued_count: int
    worker_capacity: int


class UserResponse(BaseModel):
    id: UUID
    display_name: str
    role: str
