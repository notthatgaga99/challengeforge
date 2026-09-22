"""Facade wiring observer → pressure → admission → adaptive concurrency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from challengeforge.domain.enums import WorkloadClass
from challengeforge.runtime.admission import AdmissionDecision, ExpensiveAdmission
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.controller import AdaptiveConcurrencyController
from challengeforge.runtime.observer import ResourceObserver, ResourceSnapshot
from challengeforge.runtime.pressure import PressureClassifier, PressureState


@dataclass
class ResourceAwareRuntime:
    budget: ResourceBudget
    enabled: bool = True
    observer: ResourceObserver = field(default_factory=ResourceObserver)
    classifier: PressureClassifier = field(default_factory=PressureClassifier)
    admission: ExpensiveAdmission = field(default_factory=ExpensiveAdmission)
    controller: AdaptiveConcurrencyController | None = None
    last_snapshot: ResourceSnapshot | None = None
    last_pressure: PressureState = PressureState.NORMAL
    interactive_p95_ms: float | None = None

    def __post_init__(self) -> None:
        if self.controller is None:
            self.controller = AdaptiveConcurrencyController(self.budget)

    def set_interactive_p95_ms(self, value: float | None) -> None:
        self.interactive_p95_ms = value

    def tick(
        self,
        *,
        queued_evaluations: int,
        running_evaluations: int,
        running_heavy: int = 0,
        peek_workload: WorkloadClass | None = None,
    ) -> dict[str, Any]:
        """Observe → classify → adjust concurrency → admission for next claim."""
        if not self.enabled:
            effective = self.budget.max_evaluation_concurrency
            decision = AdmissionDecision(True, "runtime_disabled", PressureState.NORMAL)
            return {
                "enabled": False,
                "pressure": PressureState.NORMAL.value,
                "effective_max_workers": effective,
                "admission": decision,
                "snapshot": None,
            }

        snapshot = self.observer.sample(
            queued_evaluations=queued_evaluations,
            running_evaluations=running_evaluations,
            interactive_p95_ms=self.interactive_p95_ms,
        )
        pressure = self.classifier.classify(snapshot, self.budget)
        assert self.controller is not None
        effective = self.controller.adjust(pressure=pressure, snapshot=snapshot)
        decision = self.admission.decide(
            pressure=pressure,
            budget=self.budget,
            snapshot=snapshot,
            workload_class=peek_workload,
            running_heavy=running_heavy,
            effective_max_workers=effective,
        )
        self.last_snapshot = snapshot
        self.last_pressure = pressure
        return {
            "enabled": True,
            "pressure": pressure.value,
            "effective_max_workers": effective,
            "admission": decision,
            "snapshot": snapshot,
            "controller_history_len": len(self.controller.history),
        }

    def status_dict(self) -> dict[str, Any]:
        snap = self.last_snapshot
        return {
            "enabled": self.enabled,
            "pressure": self.last_pressure.value,
            "effective_max_workers": (
                self.controller.effective_max_workers if self.controller else None
            ),
            "budget": {
                "min_evaluation_concurrency": self.budget.min_evaluation_concurrency,
                "max_evaluation_concurrency": self.budget.max_evaluation_concurrency,
                "max_concurrent_heavy": self.budget.max_concurrent_heavy,
                "cpu_low_percent": self.budget.cpu_low_percent,
                "cpu_high_percent": self.budget.cpu_high_percent,
                "memory_soft_mb": self.budget.memory_soft_mb,
                "memory_hard_mb": self.budget.memory_hard_mb,
            },
            "snapshot": None
            if snap is None
            else {
                "cpu_percent": snap.cpu_percent,
                "rss_mb": snap.rss_mb,
                "queued_evaluations": snap.queued_evaluations,
                "running_evaluations": snap.running_evaluations,
                "interactive_p95_ms": snap.interactive_p95_ms,
            },
        }
