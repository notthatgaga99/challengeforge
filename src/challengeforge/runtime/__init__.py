"""Resource-aware runtime for the expensive evaluation plane.

Protects interactive capacity by bounding and adapting expensive work.
Submissions remain durable; only evaluation *start* is gated.
"""

from __future__ import annotations

from challengeforge.runtime.admission import ExpensiveAdmission, AdmissionDecision
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.controller import AdaptiveConcurrencyController
from challengeforge.runtime.expensive_work import ExpensiveWorkSpec, WorkCostClass
from challengeforge.runtime.interactive_signal import (
    InteractiveFeedbackController,
    InteractiveLevel,
    RollingLatencyWindow,
    is_interactive_control_path,
    merge_pressure,
)
from challengeforge.runtime.observer import ResourceObserver, ResourceSnapshot
from challengeforge.runtime.pressure import PressureClassifier, PressureState
from challengeforge.runtime.runtime import ResourceAwareRuntime

__all__ = [
    "AdmissionDecision",
    "AdaptiveConcurrencyController",
    "ExpensiveAdmission",
    "ExpensiveWorkSpec",
    "InteractiveFeedbackController",
    "InteractiveLevel",
    "PressureClassifier",
    "PressureState",
    "ResourceAwareRuntime",
    "ResourceBudget",
    "ResourceObserver",
    "ResourceSnapshot",
    "RollingLatencyWindow",
    "WorkCostClass",
    "is_interactive_control_path",
    "merge_pressure",
]
