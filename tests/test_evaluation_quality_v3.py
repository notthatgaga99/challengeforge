"""v3 quality / safe-exit / adversarial workload tests."""

from uuid import uuid4

from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
from challengeforge.evaluation.progressive import ProgressiveEvaluator
from challengeforge.evaluation.quality import compare_to_ground_truth
from challengeforge.evaluation.workload_v3 import (
    Decision,
    WorkloadKind,
    cheap_evidence,
    ground_truth_for,
)
from challengeforge.runtime.pressure import PressureState


def test_easy_pass_safe_early_exit_agrees():
    sid = uuid4()
    q = compare_to_ground_truth(
        submission_id=sid,
        metadata={"workload_kind": "easy_pass"},
        mode=EvaluationMode.FIXED_PROGRESSIVE,
    )
    assert q.agreement
    assert q.early_exit
    assert q.actual_cost_units == 1
    assert not q.false_early_pass


def test_adversarial_no_false_early_pass_under_safe_contract():
    sid = uuid4()
    # Cheap is confidently wrong but unsafe.
    ev = cheap_evidence(WorkloadKind.ADVERSARIAL_PASS, sid)
    assert ev.safe_to_terminate is False
    assert ev.provisional_decision != ground_truth_for(
        WorkloadKind.ADVERSARIAL_PASS, sid
    ).decision
    q = compare_to_ground_truth(
        submission_id=sid,
        metadata={"workload_kind": "adversarial_pass"},
        mode=EvaluationMode.FIXED_PROGRESSIVE,
    )
    assert q.agreement
    assert not q.false_early_pass
    assert q.actual_cost_units == ALWAYS_EXPENSIVE_COST_UNITS


def test_hard_requires_heavy():
    q = compare_to_ground_truth(
        submission_id=uuid4(),
        metadata={"workload_kind": "hard_fail"},
        mode=EvaluationMode.FIXED_PROGRESSIVE,
    )
    assert q.agreement
    assert q.stages == 3
    assert q.ground_truth == Decision.FAIL


def test_ambiguous_exits_at_medium():
    q = compare_to_ground_truth(
        submission_id=uuid4(),
        metadata={"workload_kind": "ambiguous_pass"},
        mode=EvaluationMode.FIXED_PROGRESSIVE,
    )
    assert q.agreement
    assert q.actual_cost_units == 1 + 3
    assert q.stages == 2


def test_degraded_defers_then_agrees_after_resume():
    sid = uuid4()
    first = ProgressiveEvaluator(EvaluationMode.RESOURCE_AWARE_ADAPTIVE).run(
        submission_id=sid,
        metadata={"workload_kind": "hard_pass"},
        artifact_key=None,
        pressure=PressureState.DEGRADED,
    )
    assert not first.finished
    q = compare_to_ground_truth(
        submission_id=sid,
        metadata={"workload_kind": "hard_pass"},
        mode=EvaluationMode.RESOURCE_AWARE_ADAPTIVE,
        pressure=PressureState.DEGRADED,
    )
    assert q.agreement
