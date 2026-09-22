"""Progressive escalation policy (deterministic, inspectable)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import EvaluationMode
from challengeforge.runtime.pressure import PressureState


class EscalationAction(StrEnum):
    FINISH = "finish"
    CONTINUE = "continue"
    DEFER = "defer"  # requeue for later expensive stage


@dataclass(frozen=True)
class EscalationDecision:
    action: EscalationAction
    reason: str


@dataclass(frozen=True)
class ProgressivePolicy:
    mode: EvaluationMode

    def decide(
        self,
        *,
        confidence: StageConfidence,
        next_stage_exists: bool,
        pressure: PressureState,
        deadline_at: datetime | None,
        now: datetime | None = None,
        next_stage_cost_units: int = 0,
    ) -> EscalationDecision:
        if self.mode == EvaluationMode.ALWAYS_EXPENSIVE:
            if next_stage_exists:
                return EscalationDecision(EscalationAction.CONTINUE, "always_expensive")
            return EscalationDecision(EscalationAction.FINISH, "plan_exhausted")

        # fixed_progressive and resource_aware_adaptive share confidence early-exit
        if confidence in (
            StageConfidence.PASS_CONFIDENT,
            StageConfidence.FAIL_CONFIDENT,
        ):
            return EscalationDecision(
                EscalationAction.FINISH, f"early_exit_{confidence.value}"
            )

        if not next_stage_exists:
            return EscalationDecision(EscalationAction.FINISH, "plan_exhausted_uncertain")

        if self.mode == EvaluationMode.FIXED_PROGRESSIVE:
            return EscalationDecision(EscalationAction.CONTINUE, "uncertain_escalate")

        # resource_aware_adaptive
        assert self.mode == EvaluationMode.RESOURCE_AWARE_ADAPTIVE
        now = now or datetime.now(timezone.utc)
        deadline_near = False
        if deadline_at is not None:
            remaining = (deadline_at - now).total_seconds()
            deadline_near = remaining <= 30.0

        if pressure == PressureState.DEGRADED and next_stage_cost_units >= 9 and not deadline_near:
            return EscalationDecision(
                EscalationAction.DEFER, "degraded_defer_heavy"
            )
        if pressure == PressureState.PRESSURED and next_stage_cost_units >= 9 and not deadline_near:
            return EscalationDecision(
                EscalationAction.DEFER, "pressured_defer_heavy"
            )
        if deadline_near:
            return EscalationDecision(EscalationAction.CONTINUE, "deadline_near_escalate")
        return EscalationDecision(EscalationAction.CONTINUE, "uncertain_escalate")
