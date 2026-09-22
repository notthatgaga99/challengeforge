"""Facade wiring observer → pressure → admission → adaptive concurrency."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from challengeforge.domain.enums import WorkloadClass
from challengeforge.runtime.admission import AdmissionDecision, ExpensiveAdmission
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.controller import AdaptiveConcurrencyController
from challengeforge.runtime.interactive_signal import (
    InteractiveFeedbackController,
    InteractiveLevel,
    merge_pressure,
)
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
    interactive_feedback: InteractiveFeedbackController | None = None
    last_snapshot: ResourceSnapshot | None = None
    last_pressure: PressureState = PressureState.NORMAL
    last_interactive_level: InteractiveLevel = InteractiveLevel.HEALTHY
    interactive_p95_ms: float | None = None
    interactive_sample_count: int = 0

    def __post_init__(self) -> None:
        if self.controller is None:
            self.controller = AdaptiveConcurrencyController(self.budget)
        if self.interactive_feedback is None:
            self.interactive_feedback = InteractiveFeedbackController(
                warn_ms=self.budget.interactive_p95_warn_ms,
                critical_ms=self.budget.interactive_p95_critical_ms,
                recovery_ms=self.budget.interactive_p95_recovery_ms,
                min_samples=self.budget.interactive_min_samples,
                sustain_seconds=self.budget.interactive_sustain_seconds,
                cooldown_seconds=max(0.5, self.budget.adjust_cooldown_seconds),
            )

    def set_interactive_p95_ms(
        self, value: float | None, *, sample_count: int = 0
    ) -> None:
        self.interactive_p95_ms = value
        self.interactive_sample_count = max(0, int(sample_count))

    def tick(
        self,
        *,
        queued_evaluations: int,
        running_evaluations: int,
        running_heavy: int = 0,
        peek_workload: WorkloadClass | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Observe → classify → interactive floor → adjust → admission."""
        if not self.enabled:
            effective = self.budget.max_evaluation_concurrency
            decision = AdmissionDecision(True, "runtime_disabled", PressureState.NORMAL)
            return {
                "enabled": False,
                "pressure": PressureState.NORMAL.value,
                "interactive_level": InteractiveLevel.HEALTHY.value,
                "effective_max_workers": effective,
                "effective_max_heavy": self.budget.max_concurrent_heavy,
                "admission": decision,
                "snapshot": None,
            }

        assert self.interactive_feedback is not None
        use_feedback = self.interactive_feedback.enabled
        level = self.interactive_feedback.update(
            p95_ms=self.interactive_p95_ms,
            sample_count=self.interactive_sample_count,
            now=now,
        )
        self.last_interactive_level = level

        snapshot = self.observer.sample(
            queued_evaluations=queued_evaluations,
            running_evaluations=running_evaluations,
            # When hysteresis feedback is on, do not also trip classifier immediately.
            interactive_p95_ms=(
                None if use_feedback else self.interactive_p95_ms
            ),
        )
        resource_pressure = self.classifier.classify(snapshot, self.budget)
        if use_feedback:
            pressure = PressureState(
                merge_pressure(resource_pressure.value, level)
            )
        else:
            pressure = resource_pressure

        assert self.controller is not None
        effective = self.controller.adjust(
            pressure=pressure, snapshot=snapshot, now=now
        )

        effective_max_heavy = self.budget.max_concurrent_heavy
        if pressure == PressureState.DEGRADED or level == InteractiveLevel.CRITICAL:
            effective_max_heavy = 0

        decision = self.admission.decide(
            pressure=pressure,
            budget=self.budget,
            snapshot=snapshot,
            workload_class=peek_workload,
            running_heavy=running_heavy,
            effective_max_workers=effective,
            interactive_level=level,
            critical_hold_all=self.budget.interactive_critical_hold_all,
        )
        self.last_snapshot = snapshot
        self.last_pressure = pressure
        return {
            "enabled": True,
            "pressure": pressure.value,
            "interactive_level": level.value,
            "effective_max_workers": effective,
            "effective_max_heavy": effective_max_heavy,
            "admission": decision,
            "snapshot": snapshot,
            "controller_history_len": len(self.controller.history),
            "interactive_history_len": len(self.interactive_feedback.history),
            "interactive_p95_ms": self.interactive_p95_ms,
            "interactive_sample_count": self.interactive_sample_count,
        }

    def status_dict(self) -> dict[str, Any]:
        snap = self.last_snapshot
        return {
            "enabled": self.enabled,
            "pressure": self.last_pressure.value,
            "interactive_level": self.last_interactive_level.value,
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
                "interactive_p95_warn_ms": self.budget.interactive_p95_warn_ms,
                "interactive_p95_critical_ms": self.budget.interactive_p95_critical_ms,
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
