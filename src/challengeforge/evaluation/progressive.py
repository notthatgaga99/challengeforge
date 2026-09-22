"""Orchestrate progressive stages within one evaluation claim."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import (
    ALWAYS_EXPENSIVE_COST_UNITS,
    DEFAULT_PLAN,
    EvaluationMode,
    EvaluationPlan,
)
from challengeforge.evaluation.policy import (
    EscalationAction,
    EscalationDecision,
    ProgressivePolicy,
)
from challengeforge.evaluation.stages import StageOutcome, run_stage
from challengeforge.runtime.pressure import PressureState


@dataclass
class ProgressiveResult:
    """Outcome of running progressive evaluation (finish or defer)."""

    finished: bool
    score: int | None
    next_stage: int
    early_exit_reason: str | None
    defer_reason: str | None
    stages: list[dict[str, Any]] = field(default_factory=list)
    actual_cost_units: int = 0
    peak_alloc_bytes: int = 0
    execution_cpu_ms: float = 0.0
    final_tier: str | None = None
    confidence: StageConfidence | None = None
    result_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def compute_savings(self) -> float:
        if ALWAYS_EXPENSIVE_COST_UNITS <= 0:
            return 0.0
        return round(
            1.0 - (self.actual_cost_units / ALWAYS_EXPENSIVE_COST_UNITS), 4
        )


class ProgressiveEvaluator:
    def __init__(
        self,
        mode: EvaluationMode,
        plan: EvaluationPlan | None = None,
    ) -> None:
        self.mode = mode
        self.plan = plan or DEFAULT_PLAN
        self.policy = ProgressivePolicy(mode)

    def run(
        self,
        *,
        submission_id: UUID,
        metadata: dict[str, Any],
        artifact_key: str | None,
        start_stage: int = 0,
        pressure: PressureState = PressureState.NORMAL,
        deadline_at: datetime | None = None,
    ) -> ProgressiveResult:
        stages_log: list[dict[str, Any]] = []
        total_cpu = 0.0
        peak = 0
        cost = 0
        score = 0
        confidence: StageConfidence | None = None
        index = max(0, start_stage)

        while True:
            stage = self.plan.stage_at(index)
            if stage is None:
                break
            outcome: StageOutcome = run_stage(
                stage=stage,
                submission_id=submission_id,
                metadata=metadata,
                artifact_key=artifact_key,
                stage_index=index,
            )
            stages_log.append(outcome.evidence)
            total_cpu += outcome.execution_cpu_ms
            peak = max(peak, outcome.peak_alloc_bytes)
            cost += stage.cost_units
            score = outcome.score
            confidence = outcome.confidence

            next_stage = self.plan.stage_at(index + 1)
            decision: EscalationDecision = self.policy.decide(
                confidence=outcome.confidence,
                next_stage_exists=next_stage is not None,
                pressure=pressure,
                deadline_at=deadline_at,
                next_stage_cost_units=next_stage.cost_units if next_stage else 0,
                safe_to_terminate=outcome.safe_to_terminate,
            )
            if decision.action == EscalationAction.FINISH:
                return ProgressiveResult(
                    finished=True,
                    score=score,
                    next_stage=index + 1,
                    early_exit_reason=decision.reason,
                    defer_reason=None,
                    stages=stages_log,
                    actual_cost_units=cost,
                    peak_alloc_bytes=peak,
                    execution_cpu_ms=round(total_cpu, 3),
                    final_tier=stage.name,
                    confidence=confidence,
                    result_metadata=self._meta(
                        stages_log,
                        cost,
                        peak,
                        total_cpu,
                        stage.name,
                        decision.reason,
                        None,
                        confidence,
                    ),
                )
            if decision.action == EscalationAction.DEFER:
                return ProgressiveResult(
                    finished=False,
                    score=score,
                    next_stage=index + 1,
                    early_exit_reason=None,
                    defer_reason=decision.reason,
                    stages=stages_log,
                    actual_cost_units=cost,
                    peak_alloc_bytes=peak,
                    execution_cpu_ms=round(total_cpu, 3),
                    final_tier=stage.name,
                    confidence=confidence,
                    result_metadata=self._meta(
                        stages_log,
                        cost,
                        peak,
                        total_cpu,
                        stage.name,
                        None,
                        decision.reason,
                        confidence,
                    ),
                )
            # CONTINUE
            index += 1

        return ProgressiveResult(
            finished=True,
            score=score,
            next_stage=index,
            early_exit_reason="plan_exhausted",
            defer_reason=None,
            stages=stages_log,
            actual_cost_units=cost,
            peak_alloc_bytes=peak,
            execution_cpu_ms=round(total_cpu, 3),
            final_tier=stages_log[-1]["stage"] if stages_log else None,
            confidence=confidence,
            result_metadata=self._meta(
                stages_log,
                cost,
                peak,
                total_cpu,
                stages_log[-1]["stage"] if stages_log else None,
                "plan_exhausted",
                None,
                confidence,
            ),
        )

    def _meta(
        self,
        stages: list[dict[str, Any]],
        cost: int,
        peak: int,
        cpu_ms: float,
        final_tier: str | None,
        early_exit: str | None,
        defer: str | None,
        confidence: StageConfidence | None,
    ) -> dict[str, Any]:
        return {
            "evaluator": "progressive_synthetic_v1",
            "progressive": {
                "mode": self.mode.value,
                "stages_attempted": len(stages),
                "stages_completed": stages,
                "actual_cost_units": cost,
                "always_expensive_cost_units": ALWAYS_EXPENSIVE_COST_UNITS,
                "compute_savings": round(
                    1.0 - (cost / ALWAYS_EXPENSIVE_COST_UNITS), 4
                )
                if ALWAYS_EXPENSIVE_COST_UNITS
                else 0.0,
                "execution_cpu_ms": round(cpu_ms, 3),
                "peak_alloc_bytes": peak,
                "final_tier": final_tier,
                "early_exit_reason": early_exit,
                "defer_reason": defer,
                "confidence": confidence.value if confidence else None,
            },
        }
