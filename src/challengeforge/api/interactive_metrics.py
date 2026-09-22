"""Process-local interactive latency tracker + occasional DB publish.

Lives in the API process. Workers read published p95 via evaluation_scheduler_state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from challengeforge.runtime.interactive_signal import RollingLatencyWindow


@dataclass
class InteractiveMetricsPublisher:
    window: RollingLatencyWindow
    persist_every_samples: int = 5
    _since_persist: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_published: dict[str, Any] = field(default_factory=dict)

    def record(self, latency_ms: float) -> dict[str, Any]:
        self.window.record(latency_ms)
        self._since_persist += 1
        snap = self.window.snapshot()
        return snap

    def should_persist(self) -> bool:
        return self._since_persist >= max(1, self.persist_every_samples)

    def mark_persisted(self, snap: dict[str, Any]) -> None:
        self._since_persist = 0
        self._last_published = dict(snap)

    @property
    def last_published(self) -> dict[str, Any]:
        return dict(self._last_published)


def get_or_create_publisher(app: Any, settings: Any) -> InteractiveMetricsPublisher:
    existing = getattr(app.state, "interactive_metrics", None)
    if existing is not None:
        return existing
    publisher = InteractiveMetricsPublisher(
        window=RollingLatencyWindow(
            window_seconds=float(settings.resource_interactive_window_seconds),
        ),
        persist_every_samples=int(settings.resource_interactive_persist_every_samples),
    )
    app.state.interactive_metrics = publisher
    return publisher
