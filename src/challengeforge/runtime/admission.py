"""Admission control for the expensive plane (not for submissions)."""

from __future__ import annotations

from dataclasses import dataclass

from challengeforge.domain.enums import WorkloadClass
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.observer import ResourceSnapshot
from challengeforge.runtime.pressure import PressureState


@dataclass(frozen=True)
class AdmissionDecision:
    allowed: bool
    reason: str
    pressure: PressureState


class ExpensiveAdmission:
    """Decide whether evaluation *execution* may start.

    Submissions must already be durable and QUEUED before this gate runs.
    Workload-class filtering under pressure is applied via claim parameters
    (e.g. max_concurrent_heavy=0 when degraded) so bounded LIGHT bypass can
    still move the queue.
    """

    def decide(
        self,
        *,
        pressure: PressureState,
        budget: ResourceBudget,
        snapshot: ResourceSnapshot,
        workload_class: WorkloadClass | None = None,
        running_heavy: int = 0,
        effective_max_workers: int = 1,
    ) -> AdmissionDecision:
        _ = workload_class, running_heavy  # reserved for future class-aware gates
        if snapshot.running_evaluations >= max(1, effective_max_workers):
            return AdmissionDecision(
                False, "global_concurrency_full", pressure
            )

        if (
            pressure == PressureState.DEGRADED
            and snapshot.rss_mb is not None
            and snapshot.rss_mb >= budget.memory_hard_mb
            and snapshot.running_evaluations > 0
        ):
            # Memory hard cap with work already in flight: do not start more.
            return AdmissionDecision(False, "degraded_memory_hold", pressure)

        return AdmissionDecision(True, "admitted", pressure)
