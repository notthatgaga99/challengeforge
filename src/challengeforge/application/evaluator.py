"""Deterministic placeholder evaluator with experimental workload classes.

Workload classes are controlled experiment profiles, not production estimates.
CPU and memory work are bounded for laptop safety.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from challengeforge.domain.enums import WorkloadClass


class EvaluationFailed(Exception):
    """Raised by the fake evaluator to exercise the FAILED path."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class WorkloadProfile:
    """Bounded experimental cost profile."""

    cpu_iterations: int
    memory_bytes: int
    min_duration_ms: int


# Modest defaults — safe on a laptop; never unbounded.
WORKLOAD_PROFILES: dict[WorkloadClass, WorkloadProfile] = {
    WorkloadClass.LIGHT: WorkloadProfile(
        cpu_iterations=25_000,
        memory_bytes=1 * 1024 * 1024,
        min_duration_ms=20,
    ),
    WorkloadClass.MEDIUM: WorkloadProfile(
        cpu_iterations=120_000,
        memory_bytes=8 * 1024 * 1024,
        min_duration_ms=80,
    ),
    WorkloadClass.HEAVY: WorkloadProfile(
        cpu_iterations=350_000,
        memory_bytes=32 * 1024 * 1024,
        min_duration_ms=200,
    ),
}

# Hard caps for experiment safety (bytes / iterations).
MAX_MEMORY_BYTES = 48 * 1024 * 1024
MAX_CPU_ITERATIONS = 500_000


@dataclass(frozen=True)
class EvaluationOutcome:
    score: int
    result_metadata: dict[str, Any]
    workload_class: WorkloadClass
    execution_cpu_ms: float
    peak_alloc_bytes: int


def parse_workload_class(raw: object) -> WorkloadClass:
    if raw is None:
        return WorkloadClass.LIGHT
    if isinstance(raw, WorkloadClass):
        return raw
    text = str(raw).strip().lower()
    try:
        return WorkloadClass(text)
    except ValueError:
        return WorkloadClass.LIGHT


def _bounded_cpu_work(iterations: int, seed: bytes) -> str:
    digest = hashlib.sha256(seed).digest()
    n = min(max(iterations, 1), MAX_CPU_ITERATIONS)
    for i in range(n):
        digest = hashlib.sha256(digest + i.to_bytes(4, "little")).digest()
    return digest.hex()


def _bounded_memory_work(nbytes: int) -> int:
    size = min(max(nbytes, 0), MAX_MEMORY_BYTES)
    if size <= 0:
        return 0
    # Allocate, touch pages, then drop reference so GC can reclaim.
    block = bytearray(size)
    step = 4096
    for offset in range(0, size, step):
        block[offset] = (offset // step) % 256
    del block
    return size


def evaluate_submission(
    *,
    submission_id: UUID,
    metadata: dict[str, Any],
    artifact_key: str | None,
    workload_class: WorkloadClass | str | None = None,
) -> EvaluationOutcome:
    """Map submission characteristics to a deterministic score + bounded work.

    Failure trigger (test / experiment only):
        metadata["force_evaluation_failure"] == true

    Workload class (experiment only):
        metadata["workload_class"] in {light, medium, heavy}
        or explicit workload_class argument
    """
    if metadata.get("force_evaluation_failure") is True:
        raise EvaluationFailed("Forced evaluation failure via submission metadata.")

    cls = parse_workload_class(
        workload_class if workload_class is not None else metadata.get("workload_class")
    )
    profile = WORKLOAD_PROFILES[cls]

    started = time.perf_counter()
    peak_alloc = _bounded_memory_work(profile.memory_bytes)
    cpu_token = _bounded_cpu_work(
        profile.cpu_iterations,
        f"{submission_id}:{cls.value}".encode("utf-8"),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    remaining = profile.min_duration_ms - elapsed_ms
    if remaining > 0:
        time.sleep(remaining / 1000.0)
    cpu_ms = (time.perf_counter() - started) * 1000.0

    canonical = json.dumps(
        {
            "submission_id": str(submission_id),
            "metadata": metadata,
            "artifact_key": artifact_key,
            "workload_class": cls.value,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical + cpu_token.encode("ascii")).hexdigest()
    score = int(digest[:8], 16) % 101
    return EvaluationOutcome(
        score=score,
        workload_class=cls,
        execution_cpu_ms=round(cpu_ms, 3),
        peak_alloc_bytes=peak_alloc,
        result_metadata={
            "evaluator": "deterministic_placeholder_v2",
            "fingerprint": digest[:16],
            "has_artifact": artifact_key is not None,
            "metadata_keys": sorted(metadata.keys()),
            "workload_class": cls.value,
            "cpu_iterations": profile.cpu_iterations,
            "memory_bytes": profile.memory_bytes,
            "execution_cpu_ms": round(cpu_ms, 3),
            "peak_alloc_bytes": peak_alloc,
        },
    )
