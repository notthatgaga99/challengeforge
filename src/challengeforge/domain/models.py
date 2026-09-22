from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from challengeforge.domain.enums import (
    ChallengeStatus,
    EvaluationStatus,
    HackathonStatus,
    SubmissionStatus,
    UserRole,
    WorkloadClass,
)


@dataclass(frozen=True)
class User:
    id: UUID
    display_name: str
    role: UserRole
    created_at: datetime


@dataclass(frozen=True)
class Hackathon:
    id: UUID
    title: str
    description: str
    status: HackathonStatus
    organizer_id: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ChallengeSpecification:
    body: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EvaluationCriterion:
    id: UUID
    name: str
    description: str
    weight: int


@dataclass(frozen=True)
class Challenge:
    id: UUID
    hackathon_id: UUID
    title: str
    description: str
    constraints: str
    status: ChallengeStatus
    specification: ChallengeSpecification
    evaluation_criteria: list[EvaluationCriterion]
    created_at: datetime
    updated_at: datetime

    def is_publishable(self) -> bool:
        return bool(self.specification.body.strip()) and len(self.evaluation_criteria) > 0


@dataclass(frozen=True)
class Submission:
    id: UUID
    challenge_id: UUID
    participant_id: UUID
    status: SubmissionStatus
    metadata: dict[str, Any]
    artifact_key: str | None
    idempotency_key: str | None
    request_fingerprint: str | None
    created_at: datetime
    updated_at: datetime
    failure_reason: str | None = None


@dataclass(frozen=True)
class Evaluation:
    id: UUID
    submission_id: UUID
    status: EvaluationStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    attempt_count: int
    failure_reason: str | None
    score: int | None
    result_metadata: dict[str, Any]
    worker_id: str | None = None
    workload_class: WorkloadClass = WorkloadClass.LIGHT
