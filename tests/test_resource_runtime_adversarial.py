"""Adversarial / property tests for resource-aware admission and controller."""

from challengeforge.runtime.admission import ExpensiveAdmission
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.controller import AdaptiveConcurrencyController
from challengeforge.runtime.observer import ResourceSnapshot
from challengeforge.runtime.pressure import PressureClassifier, PressureState


def _budget() -> ResourceBudget:
    return ResourceBudget(
        min_evaluation_concurrency=1,
        max_evaluation_concurrency=3,
        max_concurrent_heavy=1,
        max_queued_evaluations_soft=10,
        max_queued_evaluations_hard=50,
        cpu_low_percent=30.0,
        cpu_high_percent=70.0,
        memory_soft_mb=150.0,
        memory_hard_mb=300.0,
        adjust_cooldown_seconds=0.0,
        interactive_p95_warn_ms=200.0,
        interactive_p95_critical_ms=1000.0,
        interactive_p95_recovery_ms=160.0,
        interactive_min_samples=8,
        interactive_sustain_seconds=1.0,
        interactive_critical_hold_all=False,
    )


def test_controller_never_exceeds_max_or_goes_below_min():
    b = _budget()
    ctrl = AdaptiveConcurrencyController(b)
    for i in range(20):
        ctrl.adjust(
            pressure=PressureState.NORMAL,
            snapshot=ResourceSnapshot(
                cpu_percent=5.0,
                rss_mb=50.0,
                queued_evaluations=100,
                running_evaluations=0,
                interactive_p95_ms=None,
                sampled_at=float(i),
            ),
            now=float(i),
        )
    assert ctrl.effective_max_workers == 3
    for i in range(20, 40):
        ctrl.adjust(
            pressure=PressureState.DEGRADED,
            snapshot=ResourceSnapshot(
                cpu_percent=95.0,
                rss_mb=50.0,
                queued_evaluations=100,
                running_evaluations=2,
                interactive_p95_ms=2000.0,
                sampled_at=float(i),
            ),
            now=float(i),
        )
    assert ctrl.effective_max_workers == 1


def test_interactive_critical_forces_degraded():
    b = _budget()
    state = PressureClassifier().classify(
        ResourceSnapshot(
            cpu_percent=10.0,
            rss_mb=50.0,
            queued_evaluations=0,
            running_evaluations=0,
            interactive_p95_ms=1500.0,
            sampled_at=0.0,
        ),
        b,
    )
    assert state == PressureState.DEGRADED


def test_degraded_memory_hold_when_work_inflight():
    adm = ExpensiveAdmission()
    b = _budget()
    decision = adm.decide(
        pressure=PressureState.DEGRADED,
        budget=b,
        snapshot=ResourceSnapshot(
            cpu_percent=10.0,
            rss_mb=350.0,
            queued_evaluations=5,
            running_evaluations=1,
            interactive_p95_ms=None,
            sampled_at=0.0,
        ),
        effective_max_workers=2,
    )
    assert decision.allowed is False
    assert decision.reason == "degraded_memory_hold"
