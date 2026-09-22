"""Progressive / adaptive evaluation (synthetic stages).

Not an intelligent evaluator — architecture for spending compute selectively.
"""

from __future__ import annotations

from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import EvaluationMode, EvaluationPlan, StageSpec
from challengeforge.evaluation.policy import EscalationDecision, ProgressivePolicy
from challengeforge.evaluation.progressive import ProgressiveEvaluator, ProgressiveResult
from challengeforge.evaluation.workload_v3 import Decision, WorkloadKind

__all__ = [
    "Decision",
    "EscalationDecision",
    "EvaluationMode",
    "EvaluationPlan",
    "ProgressiveEvaluator",
    "ProgressivePolicy",
    "ProgressiveResult",
    "StageConfidence",
    "StageSpec",
    "WorkloadKind",
]
