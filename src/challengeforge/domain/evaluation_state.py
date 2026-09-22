from challengeforge.domain.enums import EvaluationStatus
from challengeforge.domain.exceptions import InvalidStateTransition

ALLOWED_TRANSITIONS: dict[EvaluationStatus, frozenset[EvaluationStatus]] = {
    EvaluationStatus.QUEUED: frozenset({EvaluationStatus.RUNNING}),
    EvaluationStatus.RUNNING: frozenset(
        {
            EvaluationStatus.SUCCEEDED,
            EvaluationStatus.FAILED,
            EvaluationStatus.QUEUED,  # crash recovery requeue
        }
    ),
    EvaluationStatus.SUCCEEDED: frozenset(),
    EvaluationStatus.FAILED: frozenset(),
}


def transition_evaluation(
    current: EvaluationStatus, target: EvaluationStatus
) -> EvaluationStatus:
    allowed = ALLOWED_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransition(
            f"Cannot transition evaluation from {current} to {target}."
        )
    return target
