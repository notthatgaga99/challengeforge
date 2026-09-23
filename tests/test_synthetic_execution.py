"""Tests for synthetic execution integration contract and disposition."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from challengeforge.execution.contract import (
    CORPUS_ALIASES,
    TRUSTED_CORPUS,
    ExecutionOutcome,
    disposition,
    outcome_from_result,
    resolve_workload,
    budget_labels,
)
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ExecutionResult, ProcessExecutor
from challengeforge.evaluation.synthetic_execution import run_synthetic_execution


def test_trusted_corpus_covers_aliases():
    for alias, name in CORPUS_ALIASES.items():
        assert name in TRUSTED_CORPUS
        assert resolve_workload(alias) == name


def test_resolve_rejects_arbitrary_source():
    with pytest.raises(ValueError, match="untrusted"):
        resolve_workload("rm -rf /; import os")


def test_budget_labels_mark_gaps():
    labels = budget_labels()
    assert labels["wall_timeout"] == "ENFORCED"
    assert labels["network_deny"] == "NOT ENFORCED"
    assert labels["cgroup_memory"] == "NOT ENFORCED"
    assert labels["peak_rss"] == "OBSERVED"


def test_disposition_success_and_deterministic_fail():
    assert disposition(ExecutionOutcome.SUCCESS, attempt_count=1, max_attempts=3)[0] == "succeeded"
    term, retry, score, reason = disposition(
        ExecutionOutcome.TIMEOUT, attempt_count=1, max_attempts=3
    )
    assert term == "failed"
    assert retry is False
    assert score is None
    assert reason and "timeout" in reason


def test_disposition_infra_requeues_then_exhausts():
    term, retry, _, _ = disposition(
        ExecutionOutcome.START_FAILURE, attempt_count=1, max_attempts=3
    )
    assert term == "requeue" and retry is True
    term2, retry2, _, _ = disposition(
        ExecutionOutcome.START_FAILURE, attempt_count=3, max_attempts=3
    )
    assert term2 == "failed" and retry2 is False


def test_run_light_through_orchestrator(tmp_path: Path):
    record = run_synthetic_execution(
        evaluation_id=uuid4(),
        submission_id=uuid4(),
        metadata={"execution_workload": "LIGHT"},
        workload_class="light",
        attempt_count=1,
        max_attempts=3,
        limits=ExecutionLimits(
            wall_timeout_seconds=5.0,
            max_stdout_bytes=32 * 1024,
            max_stderr_bytes=32 * 1024,
        ),
    )
    assert record.outcome == ExecutionOutcome.SUCCESS
    assert record.evaluation_terminal == "succeeded"
    assert record.score == 100
    assert record.result_metadata["evaluator"] == "synthetic_execution_v1"
    assert record.execution.workspace_cleaned is True
    assert record.execution.leaked_pids_after_cleanup == []


def test_run_timeout_corpus(tmp_path: Path):
    record = run_synthetic_execution(
        evaluation_id=uuid4(),
        submission_id=uuid4(),
        metadata={"execution_workload": "sleep_timeout"},
        workload_class="light",
        attempt_count=1,
        max_attempts=3,
        limits=ExecutionLimits(wall_timeout_seconds=1.5, max_stdout_bytes=4096, max_stderr_bytes=4096),
    )
    assert record.outcome == ExecutionOutcome.TIMEOUT
    assert record.evaluation_terminal == "failed"
    assert record.retryable is False


def test_run_output_limit_corpus():
    record = run_synthetic_execution(
        evaluation_id=uuid4(),
        submission_id=uuid4(),
        metadata={"execution_workload": "large_stdout"},
        workload_class="light",
        attempt_count=1,
        max_attempts=3,
        limits=ExecutionLimits(wall_timeout_seconds=5.0, max_stdout_bytes=8192, max_stderr_bytes=8192),
    )
    assert record.outcome == ExecutionOutcome.OUTPUT_LIMIT
    assert record.evaluation_terminal == "failed"


def test_cleanup_idempotent_after_orchestrated_run(tmp_path: Path):
    ex = ProcessExecutor(
        limits=ExecutionLimits(wall_timeout_seconds=5.0),
        workspace_root=tmp_path,
    )
    r = ex.run("MANY_FILES")
    assert r.workspace_cleaned
    # Idempotent on missing path
    assert ex.cleanup_only(Path(r.workspace or tmp_path / "missing")) is True
    assert ex.cleanup_only(Path(r.workspace or tmp_path / "missing")) is True


def test_outcome_from_result_cleanup_error():
    fake = ExecutionResult(
        workload="LIGHT",
        status="succeeded",
        exit_code=0,
        wall_ms=1.0,
        startup_ms=1.0,
        cleanup_ms=1.0,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        peak_rss_mb=None,
        process_count_peak=1,
        leaked_pids_after_cleanup=[12345],
        workspace_cleaned=True,
    )
    assert outcome_from_result(fake) == ExecutionOutcome.CLEANUP_ERROR


def test_outcome_workspace_flake_without_leak_is_success():
    fake = ExecutionResult(
        workload="CHILD_PROCESS",
        status="succeeded",
        exit_code=0,
        wall_ms=1.0,
        startup_ms=1.0,
        cleanup_ms=1.0,
        stdout_bytes=0,
        stderr_bytes=0,
        stdout_truncated=False,
        stderr_truncated=False,
        peak_rss_mb=None,
        process_count_peak=2,
        leaked_pids_after_cleanup=[],
        workspace_cleaned=False,
    )
    assert outcome_from_result(fake) == ExecutionOutcome.SUCCESS
