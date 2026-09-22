"""Process / queue observation for the resource controller."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float | None
    rss_mb: float | None
    queued_evaluations: int
    running_evaluations: int
    interactive_p95_ms: float | None
    sampled_at: float


class ResourceObserver:
    """Sample local process resources. Queue counts are supplied by the caller."""

    def __init__(self) -> None:
        self._primed = False

    def sample(
        self,
        *,
        queued_evaluations: int,
        running_evaluations: int,
        interactive_p95_ms: float | None = None,
    ) -> ResourceSnapshot:
        cpu: float | None = None
        rss: float | None = None
        try:
            import psutil

            proc = psutil.Process()
            # First call primes; subsequent calls return useful deltas.
            measured = proc.cpu_percent(interval=None)
            if self._primed:
                cpu = float(measured)
            else:
                self._primed = True
                cpu = 0.0
            rss = round(proc.memory_info().rss / (1024 * 1024), 2)
        except Exception:
            cpu = None
            rss = None
        return ResourceSnapshot(
            cpu_percent=cpu,
            rss_mb=rss,
            queued_evaluations=max(0, int(queued_evaluations)),
            running_evaluations=max(0, int(running_evaluations)),
            interactive_p95_ms=interactive_p95_ms,
            sampled_at=time.time(),
        )
