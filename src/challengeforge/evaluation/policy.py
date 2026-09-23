"""Progressive escalation policy (deterministic, inspectable).

Early exit requires BOTH confident confidence AND safe_to_terminate=True.
"""

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
    DEFER = "defer"


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
        safe_to_terminate: bool = False,
    ) -> EscalationDecision:
        if self.mode == EvaluationMode.ALWAYS_EXPENSIVE:
            if next_stage_exists:
                return EscalationDecision(EscalationAction.CONTINUE, "always_expensive")
            return EscalationDecision(EscalationAction.FINISH, "plan_exhausted")

        confident = confidence in (
            StageConfidence.PASS_CONFIDENT,
            StageConfidence.FAIL_CONFIDENT,
        )
        # Safe early-exit contract: confident alone is insufficient.
        if confident and safe_to_terminate:
            return EscalationDecision(
                EscalationAction.FINISH, f"safe_early_exit_{confidence.value}"
            )
        if confident and not safe_to_terminate:
            # Adversarial / unsafe confident signal — must escalate if possible.
            if next_stage_exists:
                return EscalationDecision(
                    EscalationAction.CONTINUE, "unsafe_confident_escalate"
                )
            return EscalationDecision(
                EscalationAction.FINISH, "plan_exhausted_unsafe_confident"
            )

        if not next_stage_exists:
            return EscalationDecision(EscalationAction.FINISH, "plan_exhausted_uncertain")

        if self.mode == EvaluationMode.FIXED_PROGRESSIVE:
            return EscalationDecision(EscalationAction.CONTINUE, "uncertain_escalate")

        if self.mode != EvaluationMode.RESOURCE_AWARE_ADAPTIVE:
            return EscalationDecision(EscalationAction.FINISH, "unsupported_mode_finish")

        now = now or datetime.now(timezone.utc)
        deadline_near = False
        if deadline_at is not None:
            remaining = (deadline_at - now).total_seconds()
            deadline_near = remaining <= 30.0

        if pressure == PressureState.DEGRADED and next_stage_cost_units >= 9 and not deadline_near:
            return EscalationDecision(EscalationAction.DEFER, "degraded_defer_heavy")
        if pressure == PressureState.PRESSURED and next_stage_cost_units >= 9 and not deadline_near:
            return EscalationDecision(EscalationAction.DEFER, "pressured_defer_heavy")
        if deadline_near:
            return EscalationDecision(EscalationAction.CONTINUE, "deadline_near_escalate")
        return EscalationDecision(EscalationAction.CONTINUE, "uncertain_escalate")
