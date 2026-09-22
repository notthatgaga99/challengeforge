"""Evaluation plans and stage cost metadata."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from challengeforge.domain.enums import WorkloadClass


class EvaluationMode(StrEnum):
    """How a worker executes evaluation for a job."""

    LEGACY = "legacy"
    ALWAYS_EXPENSIVE = "always_expensive"
    FIXED_PROGRESSIVE = "fixed_progressive"
    RESOURCE_AWARE_ADAPTIVE = "resource_aware_adaptive"


@dataclass(frozen=True)
class StageSpec:
    name: str
    workload_class: WorkloadClass
    estimated_cpu_cost: float
    estimated_memory_mb: float
    estimated_duration_ms: int
    quality_contribution: float
    cost_units: int


CHEAP = StageSpec(
    name="cheap",
    workload_class=WorkloadClass.LIGHT,
    estimated_cpu_cost=1.0,
    estimated_memory_mb=1.0,
    estimated_duration_ms=20,
    quality_contribution=0.4,
    cost_units=1,
)
MEDIUM = StageSpec(
    name="medium",
    workload_class=WorkloadClass.MEDIUM,
    estimated_cpu_cost=3.0,
    estimated_memory_mb=8.0,
    estimated_duration_ms=80,
    quality_contribution=0.3,
    cost_units=3,
)
HEAVY = StageSpec(
    name="heavy",
    workload_class=WorkloadClass.HEAVY,
    estimated_cpu_cost=9.0,
    estimated_memory_mb=32.0,
    estimated_duration_ms=200,
    quality_contribution=0.3,
    cost_units=9,
)

ALWAYS_EXPENSIVE_COST_UNITS = CHEAP.cost_units + MEDIUM.cost_units + HEAVY.cost_units


@dataclass(frozen=True)
class EvaluationPlan:
    stages: tuple[StageSpec, ...]

    def stage_at(self, index: int) -> StageSpec | None:
        if index < 0 or index >= len(self.stages):
            return None
        return self.stages[index]


DEFAULT_PLAN = EvaluationPlan(stages=(CHEAP, MEDIUM, HEAVY))
