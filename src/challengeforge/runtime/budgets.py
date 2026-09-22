"""Explicit resource budgets for ChallengeForge.

Budgets answer: what are we protecting, and what happens when we run out?
They are configuration — not predictions. Defaults assume a laptop-sized host.
"""

from __future__ import annotations

from dataclasses import dataclass

from challengeforge.config import Settings


@dataclass(frozen=True)
class ResourceBudget:
    """Hard and soft limits for interactive vs expensive planes."""

    # Expensive-plane concurrency (evaluation workers / in-flight claims)
    min_evaluation_concurrency: int
    max_evaluation_concurrency: int
    max_concurrent_heavy: int
    max_queued_evaluations_soft: int
    max_queued_evaluations_hard: int

    # Host pressure thresholds (process-local samples)
    cpu_low_percent: float
    cpu_high_percent: float
    memory_soft_mb: float
    memory_hard_mb: float

    # Controller pacing
    adjust_cooldown_seconds: float

    # Interactive protection (optional measured hint; 0 warn/critical disables)
    interactive_p95_warn_ms: float
    interactive_p95_critical_ms: float
    interactive_p95_recovery_ms: float
    interactive_min_samples: int
    interactive_sustain_seconds: float
    interactive_critical_hold_all: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> ResourceBudget:
        max_conc = max(1, settings.evaluation_max_workers)
        min_conc = max(1, min(settings.evaluation_min_workers, max_conc))
        return cls(
            min_evaluation_concurrency=min_conc,
            max_evaluation_concurrency=max_conc,
            max_concurrent_heavy=max(
                0, min(settings.evaluation_max_concurrent_heavy, max_conc)
            ),
            max_queued_evaluations_soft=settings.evaluation_busy_queue_depth,
            max_queued_evaluations_hard=settings.evaluation_saturated_queue_depth,
            cpu_low_percent=settings.resource_cpu_low_percent,
            cpu_high_percent=settings.resource_cpu_high_percent,
            memory_soft_mb=settings.resource_memory_soft_mb,
            memory_hard_mb=settings.resource_memory_hard_mb,
            adjust_cooldown_seconds=settings.resource_adjust_cooldown_seconds,
            interactive_p95_warn_ms=settings.resource_interactive_p95_warn_ms,
            interactive_p95_critical_ms=settings.resource_interactive_p95_critical_ms,
            interactive_p95_recovery_ms=settings.resource_interactive_p95_recovery_ms,
            interactive_min_samples=settings.resource_interactive_min_samples,
            interactive_sustain_seconds=settings.resource_interactive_sustain_seconds,
            interactive_critical_hold_all=settings.resource_interactive_critical_hold_all,
        )

    def clamp_concurrency(self, value: int) -> int:
        return max(
            self.min_evaluation_concurrency,
            min(self.max_evaluation_concurrency, int(value)),
        )
