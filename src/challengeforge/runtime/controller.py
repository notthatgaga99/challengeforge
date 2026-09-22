"""Adaptive evaluation concurrency controller.

Slow, bounded, hysteresis via cooldown — not a generic cloud autoscaler.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from challengeforge.runtime.budgets import ResourceBudget
from challengeforge.runtime.observer import ResourceSnapshot
from challengeforge.runtime.pressure import PressureState


@dataclass
class AdaptiveConcurrencyController:
    budget: ResourceBudget
    effective_max_workers: int | None = None
    last_adjust_at: float | None = None
    history: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.effective_max_workers is None:
            # Start at minimum — prefer interactive headroom until backlog proves need.
            self.effective_max_workers = self.budget.min_evaluation_concurrency
        else:
            self.effective_max_workers = self.budget.clamp_concurrency(
                self.effective_max_workers
            )

    def adjust(
        self,
        *,
        pressure: PressureState,
        snapshot: ResourceSnapshot,
        now: float | None = None,
    ) -> int:
        now = time.time() if now is None else now
        current = self.budget.clamp_concurrency(self.effective_max_workers or 1)
        if (
            self.last_adjust_at is not None
            and now - self.last_adjust_at < self.budget.adjust_cooldown_seconds
        ):
            return current

        target = current
        reason = "hold"

        if pressure == PressureState.DEGRADED:
            if current > self.budget.min_evaluation_concurrency:
                target = current - 1
                reason = "degraded_decrease"
        elif pressure == PressureState.PRESSURED:
            if current > self.budget.min_evaluation_concurrency + 1:
                target = current - 1
                reason = "pressured_decrease"
            else:
                reason = "pressured_hold"
        else:
            cpu_ok = (
                snapshot.cpu_percent is None
                or snapshot.cpu_percent <= self.budget.cpu_low_percent
            )
            mem_ok = (
                snapshot.rss_mb is None
                or snapshot.rss_mb <= self.budget.memory_soft_mb
            )
            if (
                snapshot.queued_evaluations > 0
                and cpu_ok
                and mem_ok
                and current < self.budget.max_evaluation_concurrency
            ):
                target = current + 1
                reason = "normal_backlog_increase"
            else:
                reason = "normal_hold"

        target = self.budget.clamp_concurrency(target)
        if target != current:
            self.last_adjust_at = now
            self.effective_max_workers = target
            self.history.append(
                {
                    "at": now,
                    "from": current,
                    "to": target,
                    "reason": reason,
                    "pressure": pressure.value,
                    "queued": snapshot.queued_evaluations,
                    "cpu_percent": snapshot.cpu_percent,
                    "rss_mb": snapshot.rss_mb,
                }
            )
        return self.effective_max_workers or target
