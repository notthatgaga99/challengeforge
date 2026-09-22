"""Future expensive-work contract (evaluation / LLM / ingestion).

No LLM/RAG implementations here — only the inspectable cost declaration shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class WorkCostClass(StrEnum):
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"


@dataclass(frozen=True)
class ExpensiveWorkSpec:
    """What every future expensive capability should declare."""

    name: str
    cost_class: WorkCostClass
    estimated_cpu_weight: float = 1.0
    estimated_memory_mb: float = 1.0
    max_concurrency: int = 1
    retry_max: int = 3
    deadline_seconds: float | None = None
    priority: int = 100  # lower = sooner; default FIFO-friendly


class ExpensiveWork(Protocol):
    def spec(self) -> ExpensiveWorkSpec: ...
