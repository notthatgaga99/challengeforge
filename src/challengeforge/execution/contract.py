"""Execution contract: process evidence separate from evaluation decisions.

The executor reports what happened while running a trusted synthetic program.
The evaluation orchestrator decides SUCCEEDED/FAILED and whether to retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ExecutionResult
from challengeforge.execution.workloads import WORKLOAD_NAMES


class ExecutionOutcome(StrEnum):
    """Normalized process outcome (not an evaluation decision)."""

    SUCCESS = "SUCCESS"
    NONZERO_EXIT = "NONZERO_EXIT"
    TIMEOUT = "TIMEOUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    START_FAILURE = "START_FAILURE"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    CLEANUP_ERROR = "CLEANUP_ERROR"


# Trusted corpus only — never arbitrary participant source.
TRUSTED_CORPUS: dict[str, ExecutionOutcome] = {
    "LIGHT": ExecutionOutcome.SUCCESS,
    "CPU_HEAVY": ExecutionOutcome.SUCCESS,
    "MEMORY_HEAVY": ExecutionOutcome.SUCCESS,
    "SLEEP": ExecutionOutcome.SUCCESS,
    "LARGE_OUTPUT": ExecutionOutcome.OUTPUT_LIMIT,
    "CHILD_PROCESS": ExecutionOutcome.SUCCESS,
    "TIMEOUT": ExecutionOutcome.TIMEOUT,
    "MANY_FILES": ExecutionOutcome.SUCCESS,
    "FAILURE": ExecutionOutcome.NONZERO_EXIT,
}

# Alias names used in experiments / metadata (map → WORKLOAD_NAMES).
CORPUS_ALIASES: dict[str, str] = {
    "light_success": "LIGHT",
    "cpu_heavy_success": "CPU_HEAVY",
    "sleep_timeout": "TIMEOUT",  # intentional: corpus expects TIMEOUT
    "large_stdout": "LARGE_OUTPUT",
    "large_stderr": "LARGE_OUTPUT",
    "child_process": "CHILD_PROCESS",
    "nonzero_exit": "FAILURE",
    "many_files": "MANY_FILES",
    "memory_heavy": "MEMORY_HEAVY",
    "sleep_ok": "SLEEP",
}


@dataclass(frozen=True)
class ExecutionRequest:
    """What the orchestrator asks the executor to run (trusted workload name)."""

    execution_id: str
    workload: str
    limits: ExecutionLimits


@dataclass
class EvaluationExecutionRecord:
    """Bridge object: execution evidence + evaluation disposition."""

    outcome: ExecutionOutcome
    execution: ExecutionResult
    evaluation_terminal: str  # succeeded | failed | requeue
    retryable: bool
    score: int | None
    failure_reason: str | None
    result_metadata: dict[str, Any]


def resolve_workload(raw: str | None, *, workload_class: str | None = None) -> str:
    """Map metadata / alias / class fallback to a trusted WORKLOAD_NAMES entry."""
    if isinstance(raw, str) and raw.strip():
        key = raw.strip()
        if key in WORKLOAD_NAMES:
            return key
        alias = CORPUS_ALIASES.get(key) or CORPUS_ALIASES.get(key.lower())
        if alias:
            return alias
        upper = key.upper()
        if upper in WORKLOAD_NAMES:
            return upper
        raise ValueError(f"untrusted or unknown execution_workload: {raw!r}")
    # Class fallback — different vocabulary from executor names.
    mapping = {
        "light": "LIGHT",
        "medium": "SLEEP",
        "heavy": "CPU_HEAVY",
    }
    if workload_class in mapping:
        return mapping[workload_class]
    return "LIGHT"


def outcome_from_result(result: ExecutionResult) -> ExecutionOutcome:
    status = result.status
    if status == "succeeded":
        # PID leaks are a hard cleanup failure. Workspace rmtree flakiness on
        # Windows without leaks is recorded in evidence but not elevated to
        # CLEANUP_ERROR (still visible via workspace_cleaned=false).
        if result.leaked_pids_after_cleanup:
            return ExecutionOutcome.CLEANUP_ERROR
        return ExecutionOutcome.SUCCESS
    if status == "failed":
        return ExecutionOutcome.NONZERO_EXIT
    if status == "timeout":
        return ExecutionOutcome.TIMEOUT
    if status == "output_limit":
        return ExecutionOutcome.OUTPUT_LIMIT
    if status == "rss_limit":
        return ExecutionOutcome.RESOURCE_LIMIT
    if status == "error":
        return ExecutionOutcome.START_FAILURE
    return ExecutionOutcome.EXECUTOR_ERROR


def disposition(outcome: ExecutionOutcome, *, attempt_count: int, max_attempts: int) -> tuple[str, bool, int | None, str | None]:
    """Map execution outcome → (terminal, retryable, score, failure_reason).

    Returns evaluation_terminal in {succeeded, failed, requeue}.
    """
    if outcome == ExecutionOutcome.SUCCESS:
        return "succeeded", False, 100, None
    if outcome == ExecutionOutcome.CLEANUP_ERROR:
        # Process finished but cleanup incomplete — fail observably, do not score.
        return "failed", False, None, "execution_cleanup_incomplete"
    if outcome in (
        ExecutionOutcome.NONZERO_EXIT,
        ExecutionOutcome.TIMEOUT,
        ExecutionOutcome.OUTPUT_LIMIT,
        ExecutionOutcome.RESOURCE_LIMIT,
    ):
        return "failed", False, None, f"execution_{outcome.value.lower()}"
    # Infrastructure-ish
    if outcome in (ExecutionOutcome.START_FAILURE, ExecutionOutcome.EXECUTOR_ERROR):
        if attempt_count < max_attempts:
            return "requeue", True, None, f"execution_{outcome.value.lower()}_retry"
        return "failed", False, None, f"execution_{outcome.value.lower()}_exhausted"
    return "failed", False, None, f"execution_{outcome.value.lower()}"


def budget_labels() -> dict[str, str]:
    """Mandatory ENFORCED / OBSERVED / NOT ENFORCED map for this host prototype."""
    return {
        "wall_timeout": "ENFORCED",
        "stdout_cap": "ENFORCED",
        "stderr_cap": "ENFORCED",
        "process_tree_cleanup": "ENFORCED",
        "workspace_cleanup": "ENFORCED",
        "scrubbed_env": "ENFORCED",
        "peak_rss": "OBSERVED",
        "process_count": "OBSERVED",
        "cpu_accounting": "OBSERVED",
        "network_deny": "NOT ENFORCED",
        "cgroup_cpu_quota": "NOT ENFORCED",
        "cgroup_memory": "NOT ENFORCED",
        "filesystem_jail": "NOT ENFORCED",
    }
