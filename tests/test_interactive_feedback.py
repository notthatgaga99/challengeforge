"""Deterministic tests for interactive closed-loop feedback control."""

from challengeforge.domain.enums import WorkloadClass
from challengeforge.runtime.admission import ExpensiveAdmission
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.interactive_signal import (
    InteractiveFeedbackController,
    InteractiveLevel,
    RollingLatencyWindow,
    is_interactive_control_path,
    merge_pressure,
)
from challengeforge.runtime.observer import ResourceSnapshot
from challengeforge.runtime.pressure import PressureState
from challengeforge.runtime.runtime import ResourceAwareRuntime


def _budget(**overrides) -> ResourceBudget:
    base = dict(
        min_evaluation_concurrency=1,
        max_evaluation_concurrency=3,
        max_concurrent_heavy=1,
        max_queued_evaluations_soft=20,
        max_queued_evaluations_hard=100,
        cpu_low_percent=35.0,
        cpu_high_percent=75.0,
        memory_soft_mb=200.0,
        memory_hard_mb=400.0,
        adjust_cooldown_seconds=0.5,
        interactive_p95_warn_ms=300.0,
        interactive_p95_critical_ms=900.0,
        interactive_p95_recovery_ms=220.0,
        interactive_min_samples=5,
        interactive_sustain_seconds=1.0,
        interactive_critical_hold_all=False,
    )
    base.update(overrides)
    return ResourceBudget(**base)


def test_interactive_path_filter_excludes_health_and_includes_reads():
    assert is_interactive_control_path("GET", "/api/v1/challenges/abc")
    assert is_interactive_control_path("GET", "/api/v1/users/u/submissions")
    assert is_interactive_control_path(
        "GET", "/api/v1/submissions/s/evaluation"
    )
    assert is_interactive_control_path("POST", "/api/v1/challenges/c/submissions")
    assert is_interactive_control_path("POST", "/api/v1/submissions/s/submit")
    assert not is_interactive_control_path("GET", "/health")
    assert not is_interactive_control_path("GET", "/static/app.css")
    assert not is_interactive_control_path("DELETE", "/api/v1/challenges/abc")


def test_rolling_window_p95_and_trim():
    w = RollingLatencyWindow(window_seconds=1.0, max_samples=100)
    for i in range(20):
        w.record(100.0 + i, now=0.0)
    assert w.sample_count(now=0.0) == 20
    assert w.p95_ms(now=0.0) is not None
    assert w.sample_count(now=2.0) == 0


def test_feedback_insufficient_samples_hold_healthy():
    ctrl = InteractiveFeedbackController(
        warn_ms=300, critical_ms=900, recovery_ms=220, min_samples=8, sustain_seconds=0.5
    )
    assert ctrl.update(p95_ms=5000, sample_count=3, now=1.0) == InteractiveLevel.HEALTHY


def test_feedback_warn_critical_recovery_hysteresis():
    ctrl = InteractiveFeedbackController(
        warn_ms=300,
        critical_ms=900,
        recovery_ms=220,
        min_samples=5,
        sustain_seconds=1.0,
        cooldown_seconds=0.5,
    )
    # Healthy
    assert ctrl.update(p95_ms=100, sample_count=10, now=0.0) == InteractiveLevel.HEALTHY
    # Spike below sustain → still healthy
    assert ctrl.update(p95_ms=400, sample_count=10, now=0.2) == InteractiveLevel.HEALTHY
    # Sustained warn
    assert ctrl.update(p95_ms=400, sample_count=10, now=1.5) == InteractiveLevel.WARN
    # Transient dip above recovery → stay warn
    assert ctrl.update(p95_ms=250, sample_count=10, now=2.2) == InteractiveLevel.WARN
    # Sustained recovery (at/below recovery threshold)
    assert ctrl.update(p95_ms=200, sample_count=10, now=2.8) == InteractiveLevel.WARN
    assert ctrl.update(p95_ms=200, sample_count=10, now=4.0) == InteractiveLevel.HEALTHY
    # Sustained critical
    assert ctrl.update(p95_ms=1000, sample_count=10, now=4.7) == InteractiveLevel.HEALTHY
    assert ctrl.update(p95_ms=1000, sample_count=10, now=6.0) == InteractiveLevel.CRITICAL
    # Recover through hysteresis
    assert ctrl.update(p95_ms=200, sample_count=10, now=6.7) == InteractiveLevel.CRITICAL
    assert ctrl.update(p95_ms=200, sample_count=10, now=8.0) == InteractiveLevel.HEALTHY


def test_feedback_cooldown_prevents_rapid_oscillation():
    ctrl = InteractiveFeedbackController(
        warn_ms=300,
        critical_ms=900,
        recovery_ms=220,
        min_samples=5,
        sustain_seconds=0.1,
        cooldown_seconds=5.0,
    )
    assert ctrl.update(p95_ms=400, sample_count=10, now=0.0) == InteractiveLevel.HEALTHY
    assert ctrl.update(p95_ms=400, sample_count=10, now=0.2) == InteractiveLevel.WARN
    # Immediately healthy desired but cooldown blocks change
    assert ctrl.update(p95_ms=100, sample_count=10, now=0.4) == InteractiveLevel.WARN
    assert ctrl.update(p95_ms=100, sample_count=10, now=0.6) == InteractiveLevel.WARN


def test_merge_pressure_raises_floor():
    assert merge_pressure("normal", InteractiveLevel.WARN) == "pressured"
    assert merge_pressure("normal", InteractiveLevel.CRITICAL) == "degraded"
    assert merge_pressure("degraded", InteractiveLevel.WARN) == "degraded"
    assert merge_pressure("pressured", InteractiveLevel.HEALTHY) == "pressured"


def test_runtime_critical_sets_max_heavy_zero():
    rt = ResourceAwareRuntime(budget=_budget(), enabled=True)
    # Prime feedback into CRITICAL
    fb = rt.interactive_feedback
    assert fb is not None
    fb.update(p95_ms=1000, sample_count=10, now=0.0)
    fb.update(p95_ms=1000, sample_count=10, now=1.5)
    assert fb.level == InteractiveLevel.CRITICAL
    rt.set_interactive_p95_ms(1000.0, sample_count=10)
    tick = rt.tick(
        queued_evaluations=5,
        running_evaluations=0,
        peek_workload=WorkloadClass.HEAVY,
        now=3.0,
    )
    assert tick["pressure"] == PressureState.DEGRADED.value
    assert tick["effective_max_heavy"] == 0
    assert tick["interactive_level"] == InteractiveLevel.CRITICAL.value


def test_critical_hold_all_blocks_admission():
    rt = ResourceAwareRuntime(
        budget=_budget(interactive_critical_hold_all=True), enabled=True
    )
    fb = rt.interactive_feedback
    assert fb is not None
    fb.level = InteractiveLevel.CRITICAL
    rt.set_interactive_p95_ms(1200.0, sample_count=20)
    tick = rt.tick(
        queued_evaluations=3,
        running_evaluations=0,
        now=10.0,
    )
    assert tick["admission"].allowed is False
    assert tick["admission"].reason == "interactive_critical_hold_all"


def test_disabled_feedback_does_not_hold():
    rt = ResourceAwareRuntime(
        budget=_budget(
            interactive_p95_warn_ms=0.0,
            interactive_p95_critical_ms=0.0,
            interactive_critical_hold_all=True,
        ),
        enabled=True,
    )
    rt.set_interactive_p95_ms(5000.0, sample_count=100)
    tick = rt.tick(queued_evaluations=1, running_evaluations=0, now=1.0)
    assert tick["interactive_level"] == InteractiveLevel.HEALTHY.value
    assert tick["admission"].allowed is True


def test_admission_memory_hold_still_works():
    b = _budget()
    decision = ExpensiveAdmission().decide(
        pressure=PressureState.DEGRADED,
        budget=b,
        snapshot=ResourceSnapshot(
            cpu_percent=10.0,
            rss_mb=500.0,
            queued_evaluations=1,
            running_evaluations=1,
            interactive_p95_ms=None,
            sampled_at=0.0,
        ),
        effective_max_workers=2,
    )
    assert decision.allowed is False
    assert decision.reason == "degraded_memory_hold"
