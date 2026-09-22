from challengeforge.domain.enums import EvaluationStatus
from challengeforge.domain.evaluation_state import transition_evaluation
from challengeforge.domain.exceptions import InvalidStateTransition
import pytest


def test_queued_to_running():
    assert (
        transition_evaluation(EvaluationStatus.QUEUED, EvaluationStatus.RUNNING)
        == EvaluationStatus.RUNNING
    )


def test_running_to_terminal_or_requeue():
    assert (
        transition_evaluation(EvaluationStatus.RUNNING, EvaluationStatus.SUCCEEDED)
        == EvaluationStatus.SUCCEEDED
    )
    assert (
        transition_evaluation(EvaluationStatus.RUNNING, EvaluationStatus.FAILED)
        == EvaluationStatus.FAILED
    )
    assert (
        transition_evaluation(EvaluationStatus.RUNNING, EvaluationStatus.QUEUED)
        == EvaluationStatus.QUEUED
    )


def test_terminal_evaluations_reject_further_transitions():
    for current in (EvaluationStatus.SUCCEEDED, EvaluationStatus.FAILED):
        with pytest.raises(InvalidStateTransition):
            transition_evaluation(current, EvaluationStatus.RUNNING)
