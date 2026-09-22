"""Deterministic synthetic stage runners."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from challengeforge.application.evaluator import (
    EvaluationFailed,
    _bounded_cpu_work,
    _bounded_memory_work,
)
from challengeforge.evaluation.confidence import StageConfidence
from challengeforge.evaluation.plan import StageSpec


@dataclass(frozen=True)
class StageOutcome:
    stage: StageSpec
    confidence: StageConfidence
    score: int
    execution_cpu_ms: float
    peak_alloc_bytes: int
    evidence: dict[str, Any]


def _scenario_confidence(metadata: dict[str, Any], submission_id: UUID) -> StageConfidence:
    raw = metadata.get("adaptive_scenario")
    if isinstance(raw, str):
        key = raw.strip().lower()
        mapping = {
            "pass_confident": StageConfidence.PASS_CONFIDENT,
            "fail_confident": StageConfidence.FAIL_CONFIDENT,
            "uncertain": StageConfidence.UNCERTAIN,
            "requires_expensive": StageConfidence.UNCERTAIN,
        }
        if key in mapping:
            return mapping[key]
    # Deterministic bucket from id — ~50% confident, ~50% uncertain by default.
    bucket = int(hashlib.sha256(str(submission_id).encode()).hexdigest()[:8], 16) % 100
    if bucket < 25:
        return StageConfidence.PASS_CONFIDENT
    if bucket < 50:
        return StageConfidence.FAIL_CONFIDENT
    return StageConfidence.UNCERTAIN


def run_stage(
    *,
    stage: StageSpec,
    submission_id: UUID,
    metadata: dict[str, Any],
    artifact_key: str | None,
    stage_index: int,
) -> StageOutcome:
    if metadata.get("force_evaluation_failure") is True:
        raise EvaluationFailed("Forced evaluation failure via submission metadata.")
    force_stage = metadata.get("force_stage_failure")
    if force_stage is not None and str(force_stage).strip().lower() == stage.name:
        raise EvaluationFailed(f"Forced stage failure at {stage.name}.")

    started = time.perf_counter()
    # Scale synthetic work from stage estimates (bounded inside helpers).
    peak = _bounded_memory_work(int(stage.estimated_memory_mb * 1024 * 1024))
    iterations = int(25_000 * stage.estimated_cpu_cost)
    token = _bounded_cpu_work(
        iterations,
        f"{submission_id}:{stage.name}:{stage_index}".encode("utf-8"),
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    remaining = stage.estimated_duration_ms - elapsed_ms
    if remaining > 0:
        time.sleep(remaining / 1000.0)
    cpu_ms = (time.perf_counter() - started) * 1000.0

    confidence = _scenario_confidence(metadata, submission_id)
    # Later stages refine score deterministically; confident fail/pass set extremes.
    canonical = json.dumps(
        {
            "submission_id": str(submission_id),
            "stage": stage.name,
            "stage_index": stage_index,
            "metadata": metadata,
            "artifact_key": artifact_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(canonical + token.encode("ascii")).hexdigest()
    base_score = int(digest[:8], 16) % 101
    if confidence == StageConfidence.PASS_CONFIDENT and stage_index == 0:
        score = 90 + (base_score % 11)
    elif confidence == StageConfidence.FAIL_CONFIDENT and stage_index == 0:
        score = base_score % 20
    else:
        # Uncertain / later stages: mid-band refined by stage name.
        score = 40 + (base_score % 41)

    # requires_expensive stays uncertain through medium.
    if (
        str(metadata.get("adaptive_scenario", "")).strip().lower() == "requires_expensive"
        and stage.name != "heavy"
    ):
        confidence = StageConfidence.UNCERTAIN

    return StageOutcome(
        stage=stage,
        confidence=confidence,
        score=min(100, max(0, score)),
        execution_cpu_ms=round(cpu_ms, 3),
        peak_alloc_bytes=peak,
        evidence={
            "stage": stage.name,
            "stage_index": stage_index,
            "confidence": confidence.value,
            "fingerprint": digest[:16],
            "cost_units": stage.cost_units,
            "workload_class": stage.workload_class.value,
        },
    )
