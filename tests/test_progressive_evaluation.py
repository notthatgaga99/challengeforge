"""Unit tests for progressive evaluation plan/policy/savings."""

from uuid import uuid4

from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
from challengeforge.evaluation.policy import EscalationAction, ProgressivePolicy
from challengeforge.evaluation.progressive import ProgressiveEvaluator
from challengeforge.runtime.pressure import PressureState


def test_always_expensive_runs_three_stages():
    result = ProgressiveEvaluator(EvaluationMode.ALWAYS_EXPENSIVE).run(
        submission_id=uuid4(),
        metadata={"adaptive_scenario": "pass_confident"},
        artifact_key=None,
    )
    assert result.finished
    assert result.actual_cost_units == ALWAYS_EXPENSIVE_COST_UNITS
    assert len(result.stages) == 3
    assert result.compute_savings == 0.0


def test_fixed_progressive_early_exits_on_confident_pass():
    result = ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
        submission_id=uuid4(),
        metadata={"adaptive_scenario": "pass_confident"},
        artifact_key=None,
    )
    assert result.finished
    assert result.actual_cost_units == 1
    assert result.early_exit_reason == "safe_early_exit_pass_confident"
    assert result.compute_savings > 0.8


def test_fixed_progressive_escalates_uncertain():
    result = ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
        submission_id=uuid4(),
        metadata={"adaptive_scenario": "uncertain"},
        artifact_key=None,
    )
    assert result.finished
    assert result.actual_cost_units >= 1 + 3  # at least cheap+medium
    assert len(result.stages) >= 2


def test_requires_expensive_reaches_heavy():
    result = ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
        submission_id=uuid4(),
        metadata={"adaptive_scenario": "requires_expensive"},
        artifact_key=None,
    )
    assert result.finished
    assert result.final_tier == "heavy"
    assert result.actual_cost_units == ALWAYS_EXPENSIVE_COST_UNITS


def test_adaptive_defers_heavy_when_degraded():
    result = ProgressiveEvaluator(EvaluationMode.RESOURCE_AWARE_ADAPTIVE).run(
        submission_id=uuid4(),
        metadata={"workload_kind": "hard_pass"},
        artifact_key=None,
        pressure=PressureState.DEGRADED,
    )
    assert result.finished is False
    assert result.defer_reason == "degraded_defer_heavy"
    assert result.next_stage == 2


def test_policy_deadline_forces_continue_under_pressure():
    from datetime import datetime, timedelta, timezone

    decision = ProgressivePolicy(EvaluationMode.RESOURCE_AWARE_ADAPTIVE).decide(
        confidence=StageConfidence.UNCERTAIN,
        next_stage_exists=True,
        pressure=PressureState.DEGRADED,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=5),
        next_stage_cost_units=9,
        safe_to_terminate=False,
    )
    assert decision.action == EscalationAction.CONTINUE
    assert decision.reason == "deadline_near_escalate"


def test_adversarial_confident_but_unsafe_must_escalate():
    result = ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
        submission_id=uuid4(),
        metadata={"workload_kind": "adversarial_pass"},
        artifact_key=None,
    )
    assert result.finished
    assert result.final_tier == "heavy"
    assert any(s.get("notes", "").startswith("adversarial") for s in result.stages[:1])
    assert result.actual_cost_units == ALWAYS_EXPENSIVE_COST_UNITS


def test_safe_contract_rejects_confident_without_safe_flag():
    decision = ProgressivePolicy(EvaluationMode.FIXED_PROGRESSIVE).decide(
        confidence=StageConfidence.PASS_CONFIDENT,
        next_stage_exists=True,
        pressure=PressureState.NORMAL,
        deadline_at=None,
        safe_to_terminate=False,
    )
    assert decision.action == EscalationAction.CONTINUE
    assert decision.reason == "unsafe_confident_escalate"
