"""Closed-loop interactive latency feedback for the expensive plane.

Observation (API process):
  Server wall-time for designated interactive HTTP paths → rolling p95.

Policy (worker process):
  Read published p95 → hysteresis state machine → raise pressure floor /
  hold HEAVY (or all) new claims. Never cancels in-flight work.
  Never rejects submissions.

Disabled when warn/critical thresholds are 0 (product default).
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Deque


class InteractiveLevel(StrEnum):
    HEALTHY = "healthy"
    WARN = "warn"
    CRITICAL = "critical"


# Paths contributing to the interactive control signal (server wall time only).
# Excludes health, static, organizer-only admin surfaces, and evaluation workers.
INTERACTIVE_METHODS = frozenset({"GET", "POST"})


def is_interactive_control_path(method: str, path: str) -> bool:
    """Return True if this HTTP request should feed the interactive p95 signal."""
    m = method.upper()
    if m not in INTERACTIVE_METHODS:
        return False
    if not path.startswith("/api/v1/"):
        return False
    # Participant-facing interactive plane.
    if path.startswith("/api/v1/challenges/") and m == "GET":
        return True
    if "/submissions" in path and m == "POST":
        # create under challenge OR submit transition
        return True
    if path.startswith("/api/v1/users/") and "/submissions" in path and m == "GET":
        return True
    if path.startswith("/api/v1/submissions/") and path.endswith("/evaluation") and m == "GET":
        return True
    return False


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * fraction) - 1))
    return float(ordered[index])


@dataclass
class RollingLatencyWindow:
    """Fixed-time rolling window of server-side interactive latencies."""

    window_seconds: float = 5.0
    max_samples: int = 2_000
    _samples: Deque[tuple[float, float]] = field(default_factory=deque)

    def record(self, latency_ms: float, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self._samples.append((now, float(latency_ms)))
        self._trim(now)

    def _trim(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        while len(self._samples) > self.max_samples:
            self._samples.popleft()

    def sample_count(self, *, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        self._trim(now)
        return len(self._samples)

    def p95_ms(self, *, now: float | None = None) -> float | None:
        now = time.monotonic() if now is None else now
        self._trim(now)
        if not self._samples:
            return None
        return percentile([v for _, v in self._samples], 0.95)

    def snapshot(self, *, now: float | None = None) -> dict:
        now = time.monotonic() if now is None else now
        self._trim(now)
        values = [v for _, v in self._samples]
        return {
            "sample_count": len(values),
            "p50_ms": percentile(values, 0.50) if values else None,
            "p95_ms": percentile(values, 0.95) if values else None,
            "p99_ms": percentile(values, 0.99) if values else None,
            "window_seconds": self.window_seconds,
        }


@dataclass
class InteractiveFeedbackController:
    """Hysteresis + cooldown gate: raw p95 → InteractiveLevel.

    Rules:
    - Insufficient samples → hold previous level (or HEALTHY if never set).
    - Enter WARN when p95 >= warn for sustain_seconds.
    - Enter CRITICAL when p95 >= critical for sustain_seconds.
    - Recover only when p95 <= recovery for sustain_seconds (hysteresis).
    - Cooldown between level *changes* to avoid NORMAL↔WARN flapping.
    - Thresholds <= 0 disable the controller (always HEALTHY).
    """

    warn_ms: float = 0.0
    critical_ms: float = 0.0
    recovery_ms: float = 0.0
    min_samples: int = 8
    sustain_seconds: float = 1.0
    cooldown_seconds: float = 2.0
    level: InteractiveLevel = InteractiveLevel.HEALTHY
    _candidate: InteractiveLevel | None = None
    _candidate_since: float | None = None
    _last_change_at: float | None = None
    history: list[dict] = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.warn_ms > 0 and self.critical_ms > 0

    def effective_recovery_ms(self) -> float:
        if self.recovery_ms > 0:
            return self.recovery_ms
        if self.warn_ms > 0:
            return self.warn_ms * 0.8
        return 0.0

    def update(
        self,
        *,
        p95_ms: float | None,
        sample_count: int,
        now: float | None = None,
    ) -> InteractiveLevel:
        now = time.monotonic() if now is None else now
        if not self.enabled:
            self.level = InteractiveLevel.HEALTHY
            return self.level
        if sample_count < self.min_samples or p95_ms is None:
            return self.level

        desired = self._desired_level(p95_ms)
        if desired == self.level:
            self._candidate = None
            self._candidate_since = None
            return self.level

        # Cooldown after a change.
        if (
            self._last_change_at is not None
            and now - self._last_change_at < self.cooldown_seconds
        ):
            return self.level

        if self._candidate != desired:
            self._candidate = desired
            self._candidate_since = now
            return self.level

        assert self._candidate_since is not None
        if now - self._candidate_since < self.sustain_seconds:
            return self.level

        prev = self.level
        self.level = desired
        self._last_change_at = now
        self._candidate = None
        self._candidate_since = None
        self.history.append(
            {
                "at": now,
                "from": prev.value,
                "to": desired.value,
                "p95_ms": p95_ms,
                "sample_count": sample_count,
            }
        )
        return self.level

    def _desired_level(self, p95_ms: float) -> InteractiveLevel:
        recovery = self.effective_recovery_ms()
        if self.level == InteractiveLevel.CRITICAL:
            if p95_ms <= recovery:
                return InteractiveLevel.HEALTHY
            if p95_ms >= self.critical_ms:
                return InteractiveLevel.CRITICAL
            if p95_ms >= self.warn_ms:
                return InteractiveLevel.WARN
            return InteractiveLevel.HEALTHY
        if self.level == InteractiveLevel.WARN:
            if p95_ms <= recovery:
                return InteractiveLevel.HEALTHY
            if p95_ms >= self.critical_ms:
                return InteractiveLevel.CRITICAL
            return InteractiveLevel.WARN
        # HEALTHY
        if p95_ms >= self.critical_ms:
            return InteractiveLevel.CRITICAL
        if p95_ms >= self.warn_ms:
            return InteractiveLevel.WARN
        return InteractiveLevel.HEALTHY


def merge_pressure(resource: str, interactive: InteractiveLevel) -> str:
    """Raise pressure floor from interactive level; never lower resource pressure."""
    from challengeforge.runtime.pressure import PressureState

    order = {
        PressureState.NORMAL.value: 0,
        PressureState.PRESSURED.value: 1,
        PressureState.DEGRADED.value: 2,
    }
    interactive_as = {
        InteractiveLevel.HEALTHY: PressureState.NORMAL.value,
        InteractiveLevel.WARN: PressureState.PRESSURED.value,
        InteractiveLevel.CRITICAL: PressureState.DEGRADED.value,
    }[interactive]
    if order[interactive_as] >= order.get(resource, 0):
        return interactive_as
    return resource
