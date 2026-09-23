"""Unit tests for off-hot-path interactive hint publisher."""

from __future__ import annotations

import asyncio

import pytest

from challengeforge.api.interactive_metrics import InteractiveMetricsPublisher
from challengeforge.runtime.interactive_signal import RollingLatencyWindow


def _publisher(**kwargs) -> InteractiveMetricsPublisher:
    base = dict(
        window=RollingLatencyWindow(window_seconds=5.0),
        pool_wait_window=RollingLatencyWindow(window_seconds=5.0),
        publish_mode="async",
        publish_interval_ms=50.0,
        publish_min_delta_ms=25.0,
        persist_every_samples=3,
    )
    base.update(kwargs)
    return InteractiveMetricsPublisher(**base)


def test_record_wall_is_memory_only_and_marks_dirty():
    p = _publisher()
    snap = p.record_wall(120.0, pool_wait_ms=4.0)
    assert snap["sample_count"] == 1
    assert snap["dirty"] is True
    assert snap["pool_wait_p95_ms"] is not None
    assert p.publish_writes == 0


def test_material_change_respects_min_delta():
    p = _publisher(publish_min_delta_ms=25.0)
    p.record_wall(100.0)
    p._last_published = p.snapshot()
    p._dirty = True
    p.record_wall(110.0)  # +10 < 25
    assert p.material_change(p.snapshot()) is False
    p.record_wall(140.0)  # jump
    assert p.material_change(p.snapshot()) is True


def test_sync_cadence_gate():
    p = _publisher(publish_mode="sync", persist_every_samples=3)
    p.record_wall(10.0)
    p.record_wall(20.0)
    assert p.should_persist_sync() is False
    p.record_wall(30.0)
    assert p.should_persist_sync() is True


@pytest.mark.asyncio
async def test_persist_failure_does_not_raise_and_counts():
    p = _publisher()
    p.record_wall(50.0)

    async def boom() -> bool:
        raise RuntimeError("db down")

    # Force failure path via monkeypatch of session factory inside persist_now
    from challengeforge.api import interactive_metrics as mod

    class FakeFactory:
        def __call__(self):
            raise RuntimeError("no db")

    # Bypass: call persist_now with broken import path by stubbing get_session_factory
    original = None
    try:
        from challengeforge.persistence import session as sess_mod

        original = sess_mod.get_session_factory
        sess_mod.get_session_factory = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        ok = await p.persist_now()
        assert ok is False
        assert p.publish_failures >= 1
        assert p._dirty is True  # still dirty after failure
    finally:
        if original is not None:
            from challengeforge.persistence import session as sess_mod

            sess_mod.get_session_factory = original


@pytest.mark.asyncio
async def test_background_publisher_starts_only_in_async_mode():
    sync_p = _publisher(publish_mode="sync")
    sync_p.start_background()
    assert sync_p._task is None

    async_p = _publisher(publish_mode="async", publish_interval_ms=50.0)
    async_p.start_background()
    assert async_p._task is not None
    await async_p.stop_background()
    assert async_p._task is None
