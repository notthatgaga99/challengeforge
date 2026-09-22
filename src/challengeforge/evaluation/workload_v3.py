"""v3 synthetic workload kinds + ground truth + safe early-exit evidence.

Deterministic. Not ML. Not a claim of real evaluation quality.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from challengeforge.evaluation.confidence import StageConfidence


class WorkloadKind(StrEnum):
    """Increasing uncertainty; truth is fixed by kind suffix."""

    EASY_PASS = "easy_pass"
    EASY_FAIL = "easy_fail"
    AMBIGUOUS_PASS = "ambiguous_pass"
    AMBIGUOUS_FAIL = "ambiguous_fail"
    HARD_PASS = "hard_pass"
    HARD_FAIL = "hard_fail"
    ADVERSARIAL_PASS = "adversarial_pass"
    ADVERSARIAL_FAIL = "adversarial_fail"


class Decision(StrEnum):
    PASS = "pass"
    FAIL = "fail"


PASS_THRESHOLD = 60


@dataclass(frozen=True)
class GroundTruth:
    decision: Decision
    kind: WorkloadKind
    # Score the full expensive path is defined to produce.
    truth_score: int


@dataclass(frozen=True)
class StageEvidence:
    """Contractual output of a stage — confidence alone is never enough."""

    confidence: StageConfidence
    safe_to_terminate: bool
    provisional_decision: Decision | None
    score: int
    kind: WorkloadKind | None
    notes: str


def parse_workload_kind(metadata: dict[str, Any]) -> WorkloadKind | None:
    raw = metadata.get("workload_kind") or metadata.get("adaptive_scenario")
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    # Map v2 scenario labels into v3 kinds where possible.
    legacy = {
        "pass_confident": WorkloadKind.EASY_PASS,
        "fail_confident": WorkloadKind.EASY_FAIL,
        "uncertain": WorkloadKind.AMBIGUOUS_PASS,
        "requires_expensive": WorkloadKind.HARD_PASS,
    }
    if key in legacy:
        return legacy[key]
    try:
        return WorkloadKind(key)
    except ValueError:
        return None


def ground_truth_for(kind: WorkloadKind, submission_id: UUID) -> GroundTruth:
    """Deterministic truth: PASS/FAIL from kind; score from kind+id."""
    decision = Decision.PASS if kind.value.endswith("_pass") else Decision.FAIL
    digest = hashlib.sha256(f"truth:{kind.value}:{submission_id}".encode()).hexdigest()
    jitter = int(digest[:4], 16) % 15
    if decision == Decision.PASS:
        truth_score = 70 + jitter  # 70–84
    else:
        truth_score = 10 + jitter  # 10–24
    return GroundTruth(decision=decision, kind=kind, truth_score=truth_score)


def decision_from_score(score: int) -> Decision:
    return Decision.PASS if score >= PASS_THRESHOLD else Decision.FAIL


def cheap_evidence(kind: WorkloadKind, submission_id: UUID) -> StageEvidence:
    """Cheap stage: when is early exit *allowed*?"""
    truth = ground_truth_for(kind, submission_id)

    if kind in (WorkloadKind.EASY_PASS, WorkloadKind.EASY_FAIL):
        # Cheap matches truth; safe to terminate.
        conf = (
            StageConfidence.PASS_CONFIDENT
            if truth.decision == Decision.PASS
            else StageConfidence.FAIL_CONFIDENT
        )
        return StageEvidence(
            confidence=conf,
            safe_to_terminate=True,
            provisional_decision=truth.decision,
            score=truth.truth_score,
            kind=kind,
            notes="easy: cheap evidence matches ground truth",
        )

    if kind in (WorkloadKind.ADVERSARIAL_PASS, WorkloadKind.ADVERSARIAL_FAIL):
        # Cheap emits a *confident but wrong* signal — must NOT be safe.
        wrong = (
            Decision.FAIL if truth.decision == Decision.PASS else Decision.PASS
        )
        conf = (
            StageConfidence.PASS_CONFIDENT
            if wrong == Decision.PASS
            else StageConfidence.FAIL_CONFIDENT
        )
        wrong_score = 95 if wrong == Decision.PASS else 5
        return StageEvidence(
            confidence=conf,
            safe_to_terminate=False,
            provisional_decision=wrong,
            score=wrong_score,
            kind=kind,
            notes="adversarial: cheap confident signal disagrees with truth",
        )

    # Ambiguous / hard: cheap cannot decide.
    return StageEvidence(
        confidence=StageConfidence.UNCERTAIN,
        safe_to_terminate=False,
        provisional_decision=None,
        score=50,
        kind=kind,
        notes="insufficient cheap evidence",
    )


def medium_evidence(kind: WorkloadKind, submission_id: UUID) -> StageEvidence:
    truth = ground_truth_for(kind, submission_id)
    if kind in (
        WorkloadKind.AMBIGUOUS_PASS,
        WorkloadKind.AMBIGUOUS_FAIL,
    ):
        conf = (
            StageConfidence.PASS_CONFIDENT
            if truth.decision == Decision.PASS
            else StageConfidence.FAIL_CONFIDENT
        )
        return StageEvidence(
            confidence=conf,
            safe_to_terminate=True,
            provisional_decision=truth.decision,
            score=truth.truth_score,
            kind=kind,
            notes="ambiguous: medium sufficient",
        )
    # Hard / adversarial still uncertain at medium.
    return StageEvidence(
        confidence=StageConfidence.UNCERTAIN,
        safe_to_terminate=False,
        provisional_decision=None,
        score=50,
        kind=kind,
        notes="medium insufficient; need heavy",
    )


def heavy_evidence(kind: WorkloadKind, submission_id: UUID) -> StageEvidence:
    truth = ground_truth_for(kind, submission_id)
    conf = (
        StageConfidence.PASS_CONFIDENT
        if truth.decision == Decision.PASS
        else StageConfidence.FAIL_CONFIDENT
    )
    return StageEvidence(
        confidence=conf,
        safe_to_terminate=True,
        provisional_decision=truth.decision,
        score=truth.truth_score,
        kind=kind,
        notes="heavy establishes ground truth",
    )


def evidence_for_stage(
    stage_name: str, kind: WorkloadKind, submission_id: UUID
) -> StageEvidence:
    if stage_name == "cheap":
        return cheap_evidence(kind, submission_id)
    if stage_name == "medium":
        return medium_evidence(kind, submission_id)
    return heavy_evidence(kind, submission_id)
