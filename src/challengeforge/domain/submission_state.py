from challengeforge.domain.enums import SubmissionStatus
from challengeforge.domain.exceptions import InvalidStateTransition

ALLOWED_TRANSITIONS: dict[SubmissionStatus, frozenset[SubmissionStatus]] = {
    SubmissionStatus.CREATED: frozenset(
        {
            SubmissionStatus.SUBMITTED,
            SubmissionStatus.CANCELLED,
            SubmissionStatus.FAILED,
        }
    ),
    SubmissionStatus.SUBMITTED: frozenset(),
    SubmissionStatus.CANCELLED: frozenset(),
    SubmissionStatus.FAILED: frozenset(),
}


def transition_submission(
    current: SubmissionStatus, target: SubmissionStatus
) -> SubmissionStatus:
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransition(
            f"Cannot transition submission from {current} to {target}."
        )
    return target
