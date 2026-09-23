"""Execution contract: process evidence separate from evaluation decisions.

The executor reports what happened while running a trusted synthetic program.
The evaluation orchestrator decides SUCCEEDED/FAILED and whether to retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from challengeforge.execution.job_object import job_objects_available
from challengeforge.execution.limits import ExecutionLimits, budget_enforcement_matrix
from challengeforge.execution.process_executor import ExecutionResult
from challengeforge.execution.workloads import WORKLOAD_NAMES


class ExecutionOutcome(StrEnum):
    """Normalized process outcome (not an evaluation decision)."""

    SUCCESS = "SUCCESS"
    NONZERO_EXIT = "NONZERO_EXIT"
    TIMEOUT = "TIMEOUT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    PROCESS_LIMIT = "PROCESS_LIMIT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    CPU_LIMIT = "CPU_LIMIT"
    WORKSPACE_LIMIT = "WORKSPACE_LIMIT"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"  # legacy alias → prefer specific limits
    START_FAILURE = "START_FAILURE"
    EXECUTOR_ERROR = "EXECUTOR_ERROR"
    CLEANUP_ERROR = "CLEANUP_ERROR"
    ORPHAN_SUSPECTED = "ORPHAN_SUSPECTED"


# Trusted corpus only — never arbitrary participant source.
TRUSTED_CORPUS: dict[str, ExecutionOutcome] = {
    "LIGHT": ExecutionOutcome.SUCCESS,
    "CPU_HEAVY": ExecutionOutcome.SUCCESS,
    "MEMORY_HEAVY": ExecutionOutcome.SUCCESS,
    "MEMORY_GROW": ExecutionOutcome.SUCCESS,
    "SLEEP": ExecutionOutcome.SUCCESS,
    "LARGE_OUTPUT": ExecutionOutcome.OUTPUT_LIMIT,
    "CHILD_PROCESS": ExecutionOutcome.SUCCESS,
    "PROCESS_HEAVY": ExecutionOutcome.SUCCESS,
    "TIMEOUT": ExecutionOutcome.TIMEOUT,
    "MANY_FILES": ExecutionOutcome.SUCCESS,
    "DISK_HEAVY": ExecutionOutcome.SUCCESS,
    "BOUNDARY_PROBE": ExecutionOutcome.SUCCESS,
    "FAILURE": ExecutionOutcome.NONZERO_EXIT,
}

CORPUS_ALIASES: dict[str, str] = {
    "light_success": "LIGHT",
    "cpu_heavy_success": "CPU_HEAVY",
    "sleep_timeout": "TIMEOUT",
    "large_stdout": "LARGE_OUTPUT",
    "large_stderr": "LARGE_OUTPUT",
    "child_process": "CHILD_PROCESS",
    "process_heavy": "PROCESS_HEAVY",
    "nonzero_exit": "FAILURE",
    "many_files": "MANY_FILES",
    "disk_heavy": "DISK_HEAVY",
    "boundary_probe": "BOUNDARY_PROBE",
    "memory_heavy": "MEMORY_HEAVY",
    "memory_grow": "MEMORY_GROW",
    "sleep_ok": "SLEEP",
}


@dataclass(frozen=True)
class ExecutionRequest:
    execution_id: str
    workload: str
    limits: ExecutionLimits


@dataclass
class EvaluationExecutionRecord:
    outcome: ExecutionOutcome
    execution: ExecutionResult
    evaluation_terminal: str  # succeeded | failed | requeue
    retryable: bool
    score: int | None
    failure_reason: str | None
    result_metadata: dict[str, Any]


def resolve_workload(raw: str | None, *, workload_class: str | None = None) -> str:
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
    mapping = {
        "light": "LIGHT",
        "medium": "SLEEP",
        "heavy": "CPU_HEAVY",
    }
    if workload_class in mapping:
        return mapping[workload_class]
    return "LIGHT"


_STATUS_TO_OUTCOME = {
    "succeeded": ExecutionOutcome.SUCCESS,
    "failed": ExecutionOutcome.NONZERO_EXIT,
    "timeout": ExecutionOutcome.TIMEOUT,
    "output_limit": ExecutionOutcome.OUTPUT_LIMIT,
    "process_limit": ExecutionOutcome.PROCESS_LIMIT,
    "memory_limit": ExecutionOutcome.MEMORY_LIMIT,
    "cpu_limit": ExecutionOutcome.CPU_LIMIT,
    "workspace_limit": ExecutionOutcome.WORKSPACE_LIMIT,
    "rss_limit": ExecutionOutcome.MEMORY_LIMIT,
    "error": ExecutionOutcome.START_FAILURE,
}


def outcome_from_result(result: ExecutionResult) -> ExecutionOutcome:
    status = result.status
    if status == "succeeded":
        if result.leaked_pids_after_cleanup:
            return ExecutionOutcome.CLEANUP_ERROR
        return ExecutionOutcome.SUCCESS
    return _STATUS_TO_OUTCOME.get(status, ExecutionOutcome.EXECUTOR_ERROR)


_DETERMINISTIC_FAIL = (
    ExecutionOutcome.NONZERO_EXIT,
    ExecutionOutcome.TIMEOUT,
    ExecutionOutcome.OUTPUT_LIMIT,
    ExecutionOutcome.PROCESS_LIMIT,
    ExecutionOutcome.MEMORY_LIMIT,
    ExecutionOutcome.CPU_LIMIT,
    ExecutionOutcome.WORKSPACE_LIMIT,
    ExecutionOutcome.RESOURCE_LIMIT,
    ExecutionOutcome.ORPHAN_SUSPECTED,
)


def disposition(
    outcome: ExecutionOutcome, *, attempt_count: int, max_attempts: int
) -> tuple[str, bool, int | None, str | None]:
    """Map execution outcome → (terminal, retryable, score, failure_reason)."""
    if outcome == ExecutionOutcome.SUCCESS:
        return "succeeded", False, 100, None
    if outcome == ExecutionOutcome.CLEANUP_ERROR:
        return "failed", False, None, "execution_cleanup_incomplete"
    if outcome in _DETERMINISTIC_FAIL:
        return "failed", False, None, f"execution_{outcome.value.lower()}"
    if outcome in (ExecutionOutcome.START_FAILURE, ExecutionOutcome.EXECUTOR_ERROR):
        if attempt_count < max_attempts:
            return "requeue", True, None, f"execution_{outcome.value.lower()}_retry"
        return "failed", False, None, f"execution_{outcome.value.lower()}_exhausted"
    return "failed", False, None, f"execution_{outcome.value.lower()}"


def budget_labels() -> dict[str, str]:
    """ENFORCED / OBSERVED / NOT_ENFORCED / ENFORCED_THROTTLE for this host."""
    matrix = budget_enforcement_matrix(job_objects=job_objects_available())
    # Preserve prior key names expected by docs/tests.
    return {
        "wall_timeout": matrix["wall_timeout"],
        "stdout_cap": matrix["stdout_cap"],
        "stderr_cap": matrix["stderr_cap"],
        "process_tree_cleanup": matrix["process_tree_cleanup"],
        "workspace_cleanup": matrix["workspace_cleanup"],
        "scrubbed_env": matrix["scrubbed_env"],
        "peak_rss": matrix["peak_rss"],
        "process_count": matrix["process_count"],
        "job_memory": matrix["job_memory"],
        "cpu_user_time": matrix["cpu_user_time"],
        "cpu_rate": matrix["cpu_rate"],
        "cpu_accounting": matrix["cpu_accounting"],
        "workspace_bytes": matrix["workspace_bytes"],
        "kill_on_job_close": matrix["kill_on_job_close"],
        "network_deny": "NOT ENFORCED",
        "cgroup_cpu_quota": "NOT ENFORCED",
        "cgroup_memory": "NOT ENFORCED",
        "filesystem_jail": "NOT ENFORCED",
    }
