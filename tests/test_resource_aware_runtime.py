"""Unit tests for Resource-Aware Runtime budgets, pressure, controller, admission."""

from challengeforge.domain.enums import WorkloadClass
from challengeforge.runtime.admission import ExpensiveAdmission
from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.controller import AdaptiveConcurrencyController
from challengeforge.runtime.observer import ResourceSnapshot
from challengeforge.runtime.pressure import PressureClassifier, PressureState
from challengeforge.runtime.runtime import ResourceAwareRuntime
from challengeforge.runtime.coalescing import InProcessCoalescer
import asyncio


def _budget(**overrides) -> ResourceBudget:
    base = dict(
        min_evaluation_concurrency=1,
        max_evaluation_concurrency=4,
        max_concurrent_heavy=1,
        max_queued_evaluations_soft=20,
        max_queued_evaluations_hard=100,
        cpu_low_percent=35.0,
        cpu_high_percent=75.0,
        memory_soft_mb=200.0,
        memory_hard_mb=400.0,
        adjust_cooldown_seconds=0.0,
        interactive_p95_warn_ms=0.0,
        interactive_p95_critical_ms=0.0,
        interactive_p95_recovery_ms=0.0,
        interactive_min_samples=8,
        interactive_sustain_seconds=1.0,
        interactive_critical_hold_all=False,
    )
    base.update(overrides)
    return ResourceBudget(**base)


def _snap(**overrides) -> ResourceSnapshot:
    base = dict(
        cpu_percent=10.0,
        rss_mb=80.0,
        queued_evaluations=0,
        running_evaluations=0,
        interactive_p95_ms=None,
        sampled_at=0.0,
    )
    base.update(overrides)
    return ResourceSnapshot(**base)


def test_budget_clamp():
    b = _budget()
    assert b.clamp_concurrency(0) == 1
    assert b.clamp_concurrency(99) == 4


def test_pressure_normal_pressured_degraded():
    clf = PressureClassifier()
    b = _budget()
    assert clf.classify(_snap(), b) == PressureState.NORMAL
    assert clf.classify(_snap(queued_evaluations=25), b) == PressureState.PRESSURED
    assert clf.classify(_snap(cpu_percent=80), b) == PressureState.DEGRADED
    assert clf.classify(_snap(rss_mb=450), b) == PressureState.DEGRADED


def test_controller_increases_on_backlog_and_decreases_on_degraded():
    b = _budget(adjust_cooldown_seconds=0.0)
    ctrl = AdaptiveConcurrencyController(b)
    assert ctrl.effective_max_workers == 1
    n = ctrl.adjust(
        pressure=PressureState.NORMAL,
        snapshot=_snap(queued_evaluations=5, cpu_percent=10),
        now=1.0,
    )
    assert n == 2
    n = ctrl.adjust(
        pressure=PressureState.DEGRADED,
        snapshot=_snap(cpu_percent=90, queued_evaluations=5),
        now=2.0,
    )
    assert n == 1


def test_controller_respects_cooldown():
    b = _budget(adjust_cooldown_seconds=10.0)
    ctrl = AdaptiveConcurrencyController(b)
    ctrl.adjust(
        pressure=PressureState.NORMAL,
        snapshot=_snap(queued_evaluations=5, cpu_percent=10),
        now=1.0,
    )
    assert ctrl.effective_max_workers == 2
    ctrl.adjust(
        pressure=PressureState.NORMAL,
        snapshot=_snap(queued_evaluations=5, cpu_percent=10),
        now=2.0,
    )
    assert ctrl.effective_max_workers == 2  # cooldown


def test_admission_holds_when_concurrency_full():
    adm = ExpensiveAdmission()
    b = _budget()
    decision = adm.decide(
        pressure=PressureState.NORMAL,
        budget=b,
        snapshot=_snap(running_evaluations=2),
        effective_max_workers=2,
    )
    assert decision.allowed is False
    assert decision.reason == "global_concurrency_full"


def test_runtime_tick_disabled_always_admits():
    rt = ResourceAwareRuntime(budget=_budget(), enabled=False)
    out = rt.tick(queued_evaluations=100, running_evaluations=0)
    assert out["admission"].allowed is True
    assert out["enabled"] is False


def test_coalescer_runs_once_for_identical_keys():
    async def _run():
        coal = InProcessCoalescer()
        calls = {"n": 0}

        async def compute():
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return 42

        results = await asyncio.gather(
            *[coal.do("same", compute) for _ in range(20)]
        )
        assert results == [42] * 20
        assert calls["n"] == 1
        assert coal.stats.computations == 1
        assert coal.stats.joins == 19

    asyncio.run(_run())
