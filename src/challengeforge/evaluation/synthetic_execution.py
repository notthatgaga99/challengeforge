"""Opt-in synthetic execution evaluation (trusted corpus only).

Never accepts arbitrary participant source. Uses ProcessExecutor for a named
workload from the allowlisted corpus.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from challengeforge.execution.contract import (
    EvaluationExecutionRecord,
    ExecutionRequest,
    disposition,
    outcome_from_result,
    resolve_workload,
)
from challengeforge.execution.limits import ExecutionLimits
from challengeforge.execution.process_executor import ProcessExecutor


def run_synthetic_execution(
    *,
    evaluation_id: UUID,
    submission_id: UUID,
    metadata: dict[str, Any],
    workload_class: str,
    attempt_count: int,
    max_attempts: int,
    limits: ExecutionLimits | None = None,
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
    executor = ProcessExecutor(limits=request.limits)
    result = executor.run(request.workload)
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
