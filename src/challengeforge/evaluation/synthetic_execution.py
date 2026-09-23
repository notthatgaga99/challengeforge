"""Opt-in synthetic execution evaluation (trusted corpus only).

Never accepts arbitrary participant source. Uses ProcessExecutor for a named
workload from the allowlisted corpus.
"""

from __future__ import annotations

from typing import Any, Callable
from uuid import UUID

from challengeforge.execution.contract import (
    EvaluationExecutionRecord,
    ExecutionRequest,
    disposition,
    outcome_from_result,
    resolve_workload,
)
from challengeforge.execution.job_object import ownership_mechanism
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.ownership import new_attempt
from challengeforge.execution.process_executor import ProcessExecutor


def run_synthetic_execution(
    *,
    evaluation_id: UUID,
    submission_id: UUID,
    metadata: dict[str, Any],
    workload_class: str,
    attempt_count: int,
    max_attempts: int,
    worker_id: str = "unknown",
    limits: ExecutionLimits | None = None,
    use_os_ownership: bool = True,
    on_started: Callable[[dict[str, Any]], None] | None = None,
) -> EvaluationExecutionRecord:
    """Run one trusted synthetic workload and return evaluation disposition."""
    workload = resolve_workload(
        metadata.get("execution_workload") if isinstance(metadata, dict) else None,
        workload_class=workload_class,
    )
    lim = limits or ExecutionLimits()
    request = ExecutionRequest(
        execution_id=str(evaluation_id),
        workload=workload,
        limits=lim,
    )
    attempt = new_attempt(
        evaluation_id=str(evaluation_id),
        worker_id=worker_id,
        workload=workload,
        ownership=ownership_mechanism() if use_os_ownership else "pid_tracking_only",
    )

    def _started(info: dict[str, Any]) -> None:
        attempt.root_pid = info.get("root_pid")
        attempt.pgid = info.get("pgid")
        attempt.workspace = info.get("workspace")
        attempt.ownership = str(info.get("ownership") or attempt.ownership)
        if on_started is not None:
            on_started(
                {
                    **attempt.to_metadata(),
                    "root_pid": attempt.root_pid,
                    "pgid": attempt.pgid,
                    "workspace": attempt.workspace,
                    "ownership": attempt.ownership,
                }
            )

    executor = ProcessExecutor(limits=request.limits)
    result = executor.run(
        request.workload,
        use_os_ownership=use_os_ownership,
        on_started=_started,
    )
    # Fill attempt from result if on_started never fired (start failure).
    if attempt.root_pid is None:
        attempt.root_pid = result.root_pid
        attempt.workspace = result.workspace
        attempt.ownership = result.ownership

    outcome = outcome_from_result(result)
    terminal, retryable, score, reason = disposition(
        outcome, attempt_count=attempt_count, max_attempts=max_attempts
    )
    meta: dict[str, Any] = {
        "evaluator": "synthetic_execution_v1",
        "submission_id": str(submission_id),
        "execution_workload": workload,
        "execution_outcome": outcome.value,
        "execution": result.to_dict(),
        "execution_attempt": attempt.to_metadata(),
        "retryable": retryable,
        "budgets": {
            "wall_timeout_seconds": lim.wall_timeout_seconds,
            "max_stdout_bytes": lim.max_stdout_bytes,
            "max_stderr_bytes": lim.max_stderr_bytes,
        },
    }
    return EvaluationExecutionRecord(
        outcome=outcome,
        execution=result,
        evaluation_terminal=terminal,
        retryable=retryable,
        score=score,
        failure_reason=reason,
        result_metadata=meta,
    )
