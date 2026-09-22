"""Adversarial progressive evaluation cases."""

from uuid import uuid4

import pytest

from challengeforge.application.evaluator import EvaluationFailed
from challengeforge.evaluation.plan import EvaluationMode
from challengeforge.evaluation.progressive import ProgressiveEvaluator
from challengeforge.evaluation.stages import run_stage
from challengeforge.evaluation.plan import CHEAP


def test_forced_stage_failure_raises():
    with pytest.raises(EvaluationFailed):
        run_stage(
            stage=CHEAP,
            submission_id=uuid4(),
            metadata={"force_stage_failure": "cheap"},
            artifact_key=None,
            stage_index=0,
        )


def test_progressive_stage_failure_propagates():
    with pytest.raises(EvaluationFailed):
        ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
            submission_id=uuid4(),
            metadata={"force_stage_failure": "cheap", "adaptive_scenario": "uncertain"},
            artifact_key=None,
        )


def test_resume_from_later_stage_skips_cheap():
    result = ProgressiveEvaluator(EvaluationMode.ALWAYS_EXPENSIVE).run(
        submission_id=uuid4(),
        metadata={"adaptive_scenario": "uncertain"},
        artifact_key=None,
        start_stage=2,
    )
    assert result.finished
    assert len(result.stages) == 1
    assert result.stages[0]["stage"] == "heavy"
    assert result.actual_cost_units == 9
