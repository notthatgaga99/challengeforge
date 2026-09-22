"""Pressure state machine for graceful degradation."""

from __future__ import annotations

from enum import StrEnum

from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.observer import ResourceSnapshot


class PressureState(StrEnum):
    NORMAL = "normal"
    PRESSURED = "pressured"
    DEGRADED = "degraded"


class PressureClassifier:
    """Map a snapshot + budget → explicit runtime pressure state."""

    def classify(
        self, snapshot: ResourceSnapshot, budget: ResourceBudget
    ) -> PressureState:
        cpu = snapshot.cpu_percent
        rss = snapshot.rss_mb
        queued = snapshot.queued_evaluations
        interactive = snapshot.interactive_p95_ms

        hard_cpu = cpu is not None and cpu >= budget.cpu_high_percent
        hard_mem = rss is not None and rss >= budget.memory_hard_mb
        hard_queue = queued >= budget.max_queued_evaluations_hard
        hard_interactive = (
            interactive is not None
            and budget.interactive_p95_critical_ms > 0
            and interactive >= budget.interactive_p95_critical_ms
        )
        if hard_cpu or hard_mem or hard_queue or hard_interactive:
            return PressureState.DEGRADED

        soft_cpu = cpu is not None and cpu >= (budget.cpu_low_percent + budget.cpu_high_percent) / 2
        soft_mem = rss is not None and rss >= budget.memory_soft_mb
        soft_queue = queued >= budget.max_queued_evaluations_soft
        soft_interactive = (
            interactive is not None
            and budget.interactive_p95_warn_ms > 0
            and interactive >= budget.interactive_p95_warn_ms
        )
        if soft_cpu or soft_mem or soft_queue or soft_interactive:
            return PressureState.PRESSURED

        return PressureState.NORMAL
