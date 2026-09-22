"""Deterministic helpers for hard-mix pressure experiment."""

from scripts.hard_mix_pressure_experiment import (
    POLICIES,
    expand_mix,
    kind_to_workload_class,
    mix_summary,
    derive_verdict,
    summarize_scorecard,
)
from challengeforge.domain.enums import WorkloadClass
from challengeforge.evaluation.plan import EvaluationMode


def test_expand_mix_deterministic_and_sized():
    a = expand_mix("difficult", total=10, seed=7)
    b = expand_mix("difficult", total=10, seed=7)
    c = expand_mix("difficult", total=10, seed=8)
    assert a == b
    assert a != c
    assert len(a) == 10


def test_hard_90_is_mostly_hard_or_adversarial():
    kinds = expand_mix("hard_90", total=20, seed=1)
    summary = mix_summary(kinds)
    assert summary["hard_or_adversarial_share"] >= 0.85
    assert summary["easy_share"] <= 0.15


def test_kind_to_workload_class_mapping():
    assert kind_to_workload_class("easy_pass") == WorkloadClass.LIGHT
    assert kind_to_workload_class("ambiguous_fail") == WorkloadClass.MEDIUM
    assert kind_to_workload_class("hard_pass") == WorkloadClass.HEAVY
    assert kind_to_workload_class("adversarial_fail") == WorkloadClass.HEAVY


def test_policies_cover_a_b_c_d():
    assert EvaluationMode.LEGACY.value in POLICIES
    assert EvaluationMode.ALWAYS_EXPENSIVE.value in POLICIES
    assert EvaluationMode.FIXED_PROGRESSIVE.value in POLICIES
    assert EvaluationMode.RESOURCE_AWARE_ADAPTIVE.value in POLICIES


def test_derive_verdict_prefers_evidence_fields():
    scorecard = {
        "a": {
            "tag": "policy_mix",
            "mode": "fixed_progressive",
            "mix": "adversarial",
            "decision_agreement": 1.0,
            "compute_savings": 0.0,
            "false_early_pass": 0,
            "interactive_errors": 0,
            "saturated": False,
            "offered_rps": 20.0,
        },
        "b": {
            "tag": "policy_mix",
            "mode": "fixed_progressive",
            "mix": "mostly_easy",
            "decision_agreement": 1.0,
            "compute_savings": 0.8,
            "false_early_pass": 0,
            "interactive_errors": 0,
            "saturated": False,
            "offered_rps": 20.0,
        },
        "c": {
            "tag": "policy_mix",
            "mode": "resource_aware_adaptive",
            "mix": "mostly_easy",
            "decision_agreement": 1.0,
            "compute_savings": 0.75,
            "false_early_pass": 0,
            "interactive_errors": 0,
            "saturated": False,
            "offered_rps": 20.0,
        },
    }
    verdict = derive_verdict(scorecard)
    assert verdict["fixed_progressive_mean_agreement"] == 1.0
    assert verdict["false_early_pass_total"] == 0
    assert verdict["interactive_healthy_at_20rps"] is True
    assert verdict["hard_mix_mean_savings"] == 0.0
    assert verdict["adaptive_vs_fixed_savings_delta"] is not None


def test_summarize_scorecard_keeps_quality_and_interactive():
    card = summarize_scorecard(
        {
            "s1": {
                "tag": "policy_mix",
                "mix": "difficult",
                "evaluation_mode": "fixed_progressive",
                "pressure_level": "medium",
                "saturated": False,
                "interactive": {
                    "offered_rps": 20,
                    "achieved_requests_per_second": 19.5,
                    "client_latency_ms": {"p95_ms": 120},
                    "error_count": 0,
                },
                "evaluation": {
                    "completed": 10,
                    "stranded": 0,
                    "accept_latency_ms": {"p95_ms": 40},
                    "quality": {
                        "decision_agreement": 1.0,
                        "false_early_pass": 0,
                        "false_early_fail": 0,
                        "compute_savings": 0.4,
                        "early_exit_rate": 0.5,
                        "escalation_rate": 0.6,
                        "heavy_stage_invocation_rate": 0.4,
                    },
                },
            }
        }
    )
    assert card["s1"]["decision_agreement"] == 1.0
    assert card["s1"]["interactive_p95_ms"] == 120


def test_pressure_transition_defers_then_resumes_without_losing_truth():
    """DEGRADED before HEAVY must defer; resume under quality helper still agrees."""
    from uuid import uuid4

    from challengeforge.evaluation.plan import EvaluationMode
    from challengeforge.evaluation.progressive import ProgressiveEvaluator
    from challengeforge.evaluation.quality import compare_to_ground_truth
    from challengeforge.runtime.pressure import PressureState

    sid = uuid4()
    meta = {"workload_kind": "hard_pass"}
    deferred = ProgressiveEvaluator(EvaluationMode.RESOURCE_AWARE_ADAPTIVE).run(
        submission_id=sid,
        metadata=meta,
        artifact_key=None,
        pressure=PressureState.DEGRADED,
    )
    assert deferred.finished is False
    assert deferred.defer_reason == "degraded_defer_heavy"
    assert deferred.next_stage == 2
    assert deferred.actual_cost_units >= 1  # cheap/medium evidence retained
    q = compare_to_ground_truth(
        submission_id=sid,
        metadata=meta,
        mode=EvaluationMode.RESOURCE_AWARE_ADAPTIVE,
        pressure=PressureState.DEGRADED,
    )
    assert q.agreement
    assert not q.false_early_pass


def test_unsafe_uncertainty_must_escalate_not_terminate():
    from uuid import uuid4

    from challengeforge.evaluation.plan import ALWAYS_EXPENSIVE_COST_UNITS, EvaluationMode
    from challengeforge.evaluation.progressive import ProgressiveEvaluator
    from challengeforge.evaluation.workload_v3 import cheap_evidence, WorkloadKind

    sid = uuid4()
    ev = cheap_evidence(WorkloadKind.ADVERSARIAL_PASS, sid)
    assert ev.safe_to_terminate is False
    result = ProgressiveEvaluator(EvaluationMode.FIXED_PROGRESSIVE).run(
        submission_id=sid,
        metadata={"workload_kind": "adversarial_pass"},
        artifact_key=None,
    )
    assert result.finished
    assert result.final_tier == "heavy"
    assert result.actual_cost_units == ALWAYS_EXPENSIVE_COST_UNITS
    assert len(result.stages) == 3
    # Must not have terminated after cheap despite confident-wrong evidence.
    assert result.stages[0]["stage"] == "cheap"
    assert not (
        result.early_exit_reason
        and str(result.early_exit_reason).startswith("safe_early_exit")
        and len(result.stages) < 3
    )
