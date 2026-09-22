from challengeforge.domain.enums import SubmissionStatus
from challengeforge.domain.exceptions import InvalidStateTransition
from challengeforge.domain.submission_state import transition_submission
import pytest


def test_created_can_submit():
    assert (
        transition_submission(SubmissionStatus.CREATED, SubmissionStatus.SUBMITTED)
        == SubmissionStatus.SUBMITTED
    )


def test_created_can_cancel_or_fail():
    assert (
        transition_submission(SubmissionStatus.CREATED, SubmissionStatus.CANCELLED)
        == SubmissionStatus.CANCELLED
    )
    assert (
        transition_submission(SubmissionStatus.CREATED, SubmissionStatus.FAILED)
        == SubmissionStatus.FAILED
    )


def test_terminal_states_reject_further_transitions():
    for current in (
        SubmissionStatus.SUBMITTED,
        SubmissionStatus.CANCELLED,
        SubmissionStatus.FAILED,
    ):
        with pytest.raises(InvalidStateTransition):
            transition_submission(current, SubmissionStatus.SUBMITTED)
