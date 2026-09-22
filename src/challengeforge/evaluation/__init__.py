"""Progressive / adaptive evaluation (synthetic stages).

Not an intelligent evaluator — architecture for spending compute selectively.
"""

from __future__ import annotations

from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import EvaluationMode, EvaluationPlan, StageSpec
from challengeforge.evaluation.policy import EscalationDecision, ProgressivePolicy
from challengeforge.evaluation.progressive import ProgressiveEvaluator, ProgressiveResult

__all__ = [
    "EscalationDecision",
    "EvaluationMode",
    "EvaluationPlan",
    "ProgressiveEvaluator",
    "ProgressivePolicy",
    "ProgressiveResult",
    "StageConfidence",
    "StageSpec",
]
