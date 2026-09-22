from enum import StrEnum


class UserRole(StrEnum):
    ORGANIZER = "organizer"
    PARTICIPANT = "participant"


class HackathonStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"


class ChallengeStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    CLOSED = "closed"


class SubmissionStatus(StrEnum):
    CREATED = "created"
    SUBMITTED = "submitted"
    CANCELLED = "cancelled"
    FAILED = "failed"


class EvaluationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkloadClass(StrEnum):
    """Experimental evaluator profiles — not production workload claims."""

    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"
