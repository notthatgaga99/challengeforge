from challengeforge.application.evaluator import (
    MAX_MEMORY_BYTES,
    evaluate_submission,
    parse_workload_class,
)
from challengeforge.domain.enums import WorkloadClass
from uuid import uuid4


def test_parse_workload_class():
    assert parse_workload_class("LIGHT") == WorkloadClass.LIGHT
    assert parse_workload_class("medium") == WorkloadClass.MEDIUM
    assert parse_workload_class("heavy") == WorkloadClass.HEAVY
    assert parse_workload_class("nope") == WorkloadClass.LIGHT


def test_workload_classes_are_bounded_and_deterministic():
    sid = uuid4()
    light = evaluate_submission(
        submission_id=sid,
        metadata={"workload_class": "light"},
        artifact_key=None,
    )
    medium = evaluate_submission(
        submission_id=sid,
        metadata={"workload_class": "medium"},
        artifact_key=None,
    )
    heavy = evaluate_submission(
        submission_id=sid,
        metadata={"workload_class": "heavy"},
        artifact_key=None,
    )
    assert light.workload_class == WorkloadClass.LIGHT
    assert medium.peak_alloc_bytes <= MAX_MEMORY_BYTES
    assert heavy.peak_alloc_bytes <= MAX_MEMORY_BYTES
    assert heavy.execution_cpu_ms >= light.execution_cpu_ms
    again = evaluate_submission(
        submission_id=sid,
        metadata={"workload_class": "light"},
        artifact_key=None,
    )
    assert again.score == light.score
