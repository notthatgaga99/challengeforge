"""Process-local interactive metrics + off-hot-path hint publisher.

HTTP path only updates in-memory rolling windows.
A background publisher (or explicit sync flush for experiments) writes to
evaluation_scheduler_state for the worker process.

Stale-signal semantics: on publish failure, last successful hint remains;
workers keep the last value they read. Missing samples do not reset the
feedback controller to HEALTHY (see InteractiveFeedbackController).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from challengeforge.runtime.interactive_signal import RollingLatencyWindow

log = structlog.get_logger("challengeforge.interactive_metrics")

PublishMode = Literal["sync", "async"]


@dataclass
class InteractiveMetricsPublisher:
    window: RollingLatencyWindow
    pool_wait_window: RollingLatencyWindow
    publish_mode: PublishMode = "async"
    publish_interval_ms: float = 250.0
    publish_min_delta_ms: float = 25.0
    persist_every_samples: int = 5  # sync-mode sample cadence (legacy)
    _dirty: bool = False
    _generation: int = 0
    _samples_since_sync: int = 0
    _in_flight: int = 0
    _peak_in_flight: int = 0
    _publish_writes: int = 0
    _publish_failures: int = 0
    _last_published: dict[str, Any] = field(default_factory=dict)
    _task: asyncio.Task[None] | None = None
    _stop: asyncio.Event | None = None

    def record_wall(self, latency_ms: float, *, pool_wait_ms: float | None = None) -> dict[str, Any]:
        """Hot-path safe: memory only."""
        self.window.record(latency_ms)
        if pool_wait_ms is not None and pool_wait_ms >= 0:
            self.pool_wait_window.record(float(pool_wait_ms))
        self._dirty = True
        self._generation += 1
        self._samples_since_sync += 1
        return self.snapshot()

    def begin_request(self) -> None:
        self._in_flight += 1
        if self._in_flight > self._peak_in_flight:
            self._peak_in_flight = self._in_flight

    def end_request(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    def snapshot(self) -> dict[str, Any]:
        wall = self.window.snapshot()
        pool = self.pool_wait_window.snapshot()
        return {
            "sample_count": wall["sample_count"],
            "p50_ms": wall["p50_ms"],
            "p95_ms": wall["p95_ms"],
            "p99_ms": wall["p99_ms"],
            "pool_wait_p50_ms": pool["p50_ms"],
            "pool_wait_p95_ms": pool["p95_ms"],
            "pool_wait_p99_ms": pool["p99_ms"],
            "pool_wait_sample_count": pool["sample_count"],
            "in_flight": self._in_flight,
            "peak_in_flight": self._peak_in_flight,
            "generation": self._generation,
            "dirty": self._dirty,
            "publish_writes": self._publish_writes,
            "publish_failures": self._publish_failures,
            "publish_mode": self.publish_mode,
        }

    @property
    def publish_writes(self) -> int:
        return self._publish_writes

    @property
    def publish_failures(self) -> int:
        return self._publish_failures

    def should_persist_sync(self) -> bool:
        return self._samples_since_sync >= max(1, self.persist_every_samples)

    def material_change(self, snap: dict[str, Any]) -> bool:
        if not self._last_published:
            return True
        prev = self._last_published.get("p95_ms")
        cur = snap.get("p95_ms")
        if prev is None and cur is not None:
            return True
        if prev is None or cur is None:
            return bool(cur is not None)
        if abs(float(cur) - float(prev)) >= self.publish_min_delta_ms:
            return True
        prev_pw = self._last_published.get("pool_wait_p95_ms")
        cur_pw = snap.get("pool_wait_p95_ms")
        if prev_pw is None and cur_pw is not None:
            return True
        if prev_pw is not None and cur_pw is not None:
            if abs(float(cur_pw) - float(prev_pw)) >= self.publish_min_delta_ms:
                return True
        return False

    async def persist_now(self) -> bool:
        """Write current snapshot to Postgres. Safe to call from publisher loop."""
        snap = self.snapshot()
        if not self._dirty and self._last_published:
            return False
        if self._last_published and not self.material_change(snap):
            self._dirty = False
            self._samples_since_sync = 0
            return False
        try:
            from challengeforge.persistence.repositories import EvaluationRepository
            from challengeforge.persistence.session import get_session_factory

            factory = get_session_factory()
            async with factory() as session:
                await EvaluationRepository(session).persist_interactive_hint(
                    interactive_p95_ms=snap.get("p95_ms"),
                    sample_count=int(snap.get("sample_count") or 0),
                    pool_wait_p95_ms=snap.get("pool_wait_p95_ms"),
                )
                await session.commit()
            self._last_published = dict(snap)
            self._dirty = False
            self._samples_since_sync = 0
            self._publish_writes += 1
            return True
        except Exception:
            self._publish_failures += 1
            log.warning("interactive_hint_persist_failed", exc_info=True)
            return False

    async def run_publisher(self) -> None:
        assert self._stop is not None
        interval = max(0.05, self.publish_interval_ms / 1000.0)
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                pass
            if self.publish_mode == "async":
                await self.persist_now()

    def start_background(self) -> None:
        if self.publish_mode != "async":
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.run_publisher(), name="cf-interactive-hint-publisher")

    async def stop_background(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None
        # Final flush if dirty.
        if self._dirty:
            await self.persist_now()


def get_or_create_publisher(app: Any, settings: Any) -> InteractiveMetricsPublisher:
    existing = getattr(app.state, "interactive_metrics", None)
    if existing is not None:
        return existing
    window_s = float(getattr(settings, "resource_interactive_window_seconds", 5.0))
    mode = str(getattr(settings, "resource_interactive_publish_mode", "async") or "async")
    if mode not in ("sync", "async"):
        mode = "async"
    publisher = InteractiveMetricsPublisher(
        window=RollingLatencyWindow(window_seconds=window_s),
        pool_wait_window=RollingLatencyWindow(window_seconds=window_s),
        publish_mode=mode,  # type: ignore[arg-type]
        publish_interval_ms=float(
            getattr(settings, "resource_interactive_publish_interval_ms", 250.0)
        ),
        publish_min_delta_ms=float(
            getattr(settings, "resource_interactive_publish_min_delta_ms", 25.0)
        ),
        persist_every_samples=int(
            getattr(settings, "resource_interactive_persist_every_samples", 5)
        ),
    )
    app.state.interactive_metrics = publisher
    return publisher
