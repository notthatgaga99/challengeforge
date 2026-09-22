"""Decision-agreement helpers for progressive vs ground truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from challengeforge.evaluation.plan import EvaluationMode
from challengeforge.evaluation.progressive import ProgressiveEvaluator, ProgressiveResult
from challengeforge.evaluation.workload_v3 import (
    Decision,
    WorkloadKind,
    decision_from_score,
    ground_truth_for,
    parse_workload_kind,
)
from challengeforge.runtime.pressure import PressureState


@dataclass(frozen=True)
class QualityComparison:
    kind: WorkloadKind
    ground_truth: Decision
    progressive_decision: Decision
    agreement: bool
    false_early_pass: bool
    false_early_fail: bool
    early_exit: bool
    actual_cost_units: int
    stages: int


def compare_to_ground_truth(
    *,
    submission_id: UUID,
    metadata: dict[str, Any],
    mode: EvaluationMode,
    pressure: PressureState = PressureState.NORMAL,
) -> QualityComparison:
    kind = parse_workload_kind(metadata)
    if kind is None:
        raise ValueError("metadata must include workload_kind for quality comparison")
    truth = ground_truth_for(kind, submission_id)
    evaluator = ProgressiveEvaluator(mode)
    result: ProgressiveResult = evaluator.run(
        submission_id=submission_id,
        metadata=metadata,
        artifact_key=None,
        pressure=pressure,
    )
    total_cost = result.actual_cost_units
    total_stages = list(result.stages)
    early_reason = result.early_exit_reason
    # Resume deferred escalations under NORMAL so agreement is measurable.
    guard = 0
    while not result.finished and guard < 5:
        result = evaluator.run(
            submission_id=submission_id,
            metadata=metadata,
            artifact_key=None,
            start_stage=result.next_stage,
            pressure=PressureState.NORMAL,
        )
        total_cost += result.actual_cost_units
        total_stages.extend(result.stages)
        if result.early_exit_reason:
            early_reason = result.early_exit_reason
        guard += 1

    progressive_decision = decision_from_score(int(result.score or 0))
    early = bool(
        early_reason
        and early_reason.startswith("safe_early_exit")
        and len(total_stages) < 3
    )
    agreement = progressive_decision == truth.decision
    false_early_pass = (
        early
        and progressive_decision == Decision.PASS
        and truth.decision == Decision.FAIL
    )
    false_early_fail = (
        early
        and progressive_decision == Decision.FAIL
        and truth.decision == Decision.PASS
    )
    return QualityComparison(
        kind=kind,
        ground_truth=truth.decision,
        progressive_decision=progressive_decision,
        agreement=agreement,
        false_early_pass=false_early_pass,
        false_early_fail=false_early_fail,
        early_exit=early,
        actual_cost_units=total_cost,
        stages=len(total_stages),
    )
