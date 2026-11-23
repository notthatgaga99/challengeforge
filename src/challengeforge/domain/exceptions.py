class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class PermissionDenied(DomainError):
    code = "permission_denied"
    http_status = 403


class ValidationFailed(DomainError):
    code = "validation_failed"
    http_status = 422


class InvalidStateTransition(DomainError):
    code = "invalid_state_transition"
    http_status = 409


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class Unauthenticated(DomainError):
    code = "unauthenticated"
    http_status = 401


class RateLimited(DomainError):
    """Abuse budget exceeded — maps to HTTP 429."""

    code = "rate_limited"
    http_status = 429

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = max(1, int(retry_after_seconds))
