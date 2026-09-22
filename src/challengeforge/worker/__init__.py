"""Minimal evaluation worker process.

Polls PostgreSQL for QUEUED evaluations, claims them with
SELECT ... FOR UPDATE SKIP LOCKED, runs the deterministic placeholder
evaluator, and writes SUCCEEDED or FAILED.

Crash recovery: RUNNING rows whose started_at is older than the stale
threshold are requeued (or FAILED after max attempts) by the same worker loop.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from uuid import uuid4

import structlog

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from challengeforge.application.evaluator import EvaluationFailed, evaluate_submission
from challengeforge.config import Settings, get_settings
from challengeforge.domain.enums import WorkloadClass
from challengeforge.observability import configure_logging
from challengeforge.persistence.mapping import utcnow
from challengeforge.persistence.repositories import EvaluationRepository, SubmissionRepository
from challengeforge.persistence.session import dispose_engine, get_session_factory, init_engine
from challengeforge.runtime import ResourceAwareRuntime, ResourceBudget

logger = structlog.get_logger(__name__)


class EvaluationWorker:
    def __init__(
        self,
        settings: Settings,
        worker_id: str | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        runtime: ResourceAwareRuntime | None = None,
    ) -> None:
        self.settings = settings
        self.worker_id = worker_id or f"worker-{uuid4().hex[:8]}"
        self._session_factory = session_factory
        self._stop = False
        self.jobs_completed = 0
        self.jobs_failed = 0
        self.claims = 0
        self.poll_attempts = 0
        self.empty_polls = 0
        self.admission_holds = 0
        self.crash_after_claim = False
        self.active_evaluations = 0
        self.peak_active = 0
        self.resource_samples: list[dict] = []
        if runtime is not None:
            self.runtime = runtime
        elif settings.resource_aware_runtime_enabled:
            self.runtime = ResourceAwareRuntime(
                budget=ResourceBudget.from_settings(settings),
                enabled=True,
            )
        else:
            self.runtime = ResourceAwareRuntime(
                budget=ResourceBudget.from_settings(settings),
                enabled=False,
            )

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    def request_stop(self) -> None:
        self._stop = True

    def _sample_resources(self) -> dict:
        sample = {
            "worker_id": self.worker_id,
            "active_evaluations": self.active_evaluations,
            "completed": self.jobs_completed,
            "failed": self.jobs_failed,
            "claims": self.claims,
            "timestamp": time.time(),
        }
        try:
            import psutil

            proc = psutil.Process()
            sample["cpu_percent"] = proc.cpu_percent(interval=None)
            sample["rss_mb"] = round(proc.memory_info().rss / (1024 * 1024), 2)
        except Exception:
            sample["cpu_percent"] = None
            sample["rss_mb"] = None
        self.resource_samples.append(sample)
        return sample

    async def recover_stale(self) -> int:
        factory = self._factory()
        stale_before = utcnow() - timedelta(
            seconds=self.settings.evaluation_stale_after_seconds
        )
        async with factory() as session:
            repo = EvaluationRepository(session)
            recovered = await repo.recover_stale_running(
                stale_before=stale_before,
                max_attempts=self.settings.evaluation_max_attempts,
            )
            if recovered:
                await session.commit()
                for item in recovered:
                    logger.info(
                        "evaluation_recovered",
                        evaluation_id=str(item.id),
                        submission_id=str(item.submission_id),
                        worker_id=self.worker_id,
                        attempt=item.attempt_count,
                        status=item.status.value,
                        failure_reason=item.failure_reason,
                    )
            else:
                await session.rollback()
            return len(recovered)

    async def process_one(self) -> bool:
        """Claim and evaluate one job. Returns True if work was done."""
        factory = self._factory()
        self.poll_attempts += 1

        # Resource-aware gate: may delay evaluation start; never rejects submissions.
        async with factory() as session:
            plane = await EvaluationRepository(session).plane_snapshot()
            await session.rollback()
        peek = None
        if plane.get("oldest_workload_class"):
            try:
                peek = WorkloadClass(str(plane["oldest_workload_class"]))
            except ValueError:
                peek = WorkloadClass.LIGHT
        tick = self.runtime.tick(
            queued_evaluations=int(plane["queued"]),
            running_evaluations=int(plane["running"]),
            running_heavy=int(plane["running_heavy"]),
            peek_workload=peek,
        )
        admission = tick["admission"]
        effective_workers = int(tick["effective_max_workers"])
        pressure = str(tick["pressure"])
        async with factory() as session:
            await EvaluationRepository(session).persist_runtime_state(
                pressure_state=pressure,
                adaptive_max_workers=effective_workers,
            )
            await session.commit()
        if not admission.allowed:
            self.admission_holds += 1
            logger.info(
                "evaluation_admission_hold",
                worker_id=self.worker_id,
                reason=admission.reason,
                pressure=pressure,
                effective_max_workers=effective_workers,
                queued=plane["queued"],
                running=plane["running"],
            )
            return False

        # Under DEGRADED, refuse new HEAVY starts but keep bounded LIGHT bypass.
        max_heavy = self.settings.evaluation_max_concurrent_heavy
        if pressure == "degraded":
            max_heavy = 0

        async with factory() as session:
            eval_repo = EvaluationRepository(session)
            claimed = await eval_repo.claim_next(
                worker_id=self.worker_id,
                scheduling_policy=self.settings.evaluation_scheduling_policy,
                max_workers=effective_workers,
                max_concurrent_heavy=max_heavy,
                light_bypass_limit=self.settings.evaluation_light_bypass_limit,
            )
            if claimed is None:
                self.empty_polls += 1
                await session.rollback()
                return False
            await session.commit()
            self.claims += 1
            logger.info(
                "evaluation_claimed",
                evaluation_id=str(claimed.id),
                submission_id=str(claimed.submission_id),
                worker_id=self.worker_id,
                attempt=claimed.attempt_count,
                status="running",
                started_at=claimed.started_at.isoformat() if claimed.started_at else None,
            )

        if self.crash_after_claim:
            logger.warning(
                "worker_crash_injected",
                evaluation_id=str(claimed.id),
                worker_id=self.worker_id,
            )
            self.request_stop()
            return True

        # Load submission outside the claim transaction (evaluation is durable).
        # Boundary: claim committed → evaluate without holding a DB transaction →
        # write result in a separate transaction.
        async with factory() as session:
            submission = await SubmissionRepository(session).get(claimed.submission_id)
            if submission is None:
                async with factory() as fail_session:
                    result = await EvaluationRepository(fail_session).mark_failed(
                        claimed.id,
                        failure_reason="Submission missing at evaluation time.",
                        worker_id=self.worker_id,
                    )
                    await fail_session.commit()
                    self.jobs_failed += 1
                    logger.info(
                        "evaluation_failed",
                        evaluation_id=str(claimed.id),
                        submission_id=str(claimed.submission_id),
                        worker_id=self.worker_id,
                        attempt=claimed.attempt_count,
                        status="failed",
                        workload_class=claimed.workload_class.value,
                        failure_reason="Submission missing at evaluation time.",
                    )
                return True

        self.active_evaluations += 1
        self.peak_active = max(self.peak_active, self.active_evaluations)
        self._sample_resources()
        eval_started = time.perf_counter()
        try:
            try:
                outcome = await asyncio.to_thread(
                    evaluate_submission,
                    submission_id=submission.id,
                    metadata=submission.metadata,
                    artifact_key=submission.artifact_key,
                    workload_class=claimed.workload_class,
                )
            except EvaluationFailed as exc:
                async with factory() as fail_session:
                    result = await EvaluationRepository(fail_session).mark_failed(
                        claimed.id,
                        failure_reason=exc.reason,
                        result_metadata={
                            "evaluator": "deterministic_placeholder_v2",
                            "workload_class": claimed.workload_class.value,
                        },
                        worker_id=self.worker_id,
                    )
                    await fail_session.commit()
                self.jobs_failed += 1
                logger.info(
                    "evaluation_failed",
                    evaluation_id=str(claimed.id),
                    submission_id=str(claimed.submission_id),
                    worker_id=self.worker_id,
                    attempt=claimed.attempt_count,
                    status="failed" if result else "abandoned",
                    workload_class=claimed.workload_class.value,
                    failure_reason=exc.reason,
                    completed_at=(
                        result.completed_at.isoformat()
                        if result and result.completed_at
                        else None
                    ),
                )
                return True

            async with factory() as ok_session:
                result = await EvaluationRepository(ok_session).mark_succeeded(
                    claimed.id,
                    score=outcome.score,
                    result_metadata=outcome.result_metadata,
                    worker_id=self.worker_id,
                )
                await ok_session.commit()
            if result is None:
                logger.warning(
                    "evaluation_complete_skipped",
                    evaluation_id=str(claimed.id),
                    submission_id=str(claimed.submission_id),
                    worker_id=self.worker_id,
                    attempt=claimed.attempt_count,
                    reason="row no longer RUNNING for this worker",
                )
                return True
            self.jobs_completed += 1
            queue_wait = None
            execution = None
            if result.started_at and result.created_at:
                queue_wait = (result.started_at - result.created_at).total_seconds()
            if result.completed_at and result.started_at:
                execution = (result.completed_at - result.started_at).total_seconds()
            wall_ms = (time.perf_counter() - eval_started) * 1000.0
            logger.info(
                "evaluation_succeeded",
                evaluation_id=str(result.id),
                submission_id=str(result.submission_id),
                worker_id=self.worker_id,
                attempt=result.attempt_count,
                status="succeeded",
                score=result.score,
                workload_class=outcome.workload_class.value,
                queue_wait_seconds=queue_wait,
                execution_seconds=execution,
                execution_cpu_ms=outcome.execution_cpu_ms,
                peak_alloc_bytes=outcome.peak_alloc_bytes,
                wall_ms=round(wall_ms, 3),
                queued_at=result.created_at.isoformat(),
                started_at=result.started_at.isoformat() if result.started_at else None,
                completed_at=result.completed_at.isoformat() if result.completed_at else None,
            )
            return True
        finally:
            self.active_evaluations = max(0, self.active_evaluations - 1)
            self._sample_resources()

    async def run_forever(self) -> None:
        logger.info("worker_started", worker_id=self.worker_id)
        while not self._stop:
            await self.recover_stale()
            did_work = await self.process_one()
            if not did_work:
                await asyncio.sleep(self.settings.evaluation_poll_interval_seconds)
        logger.info(
            "worker_stopped",
            worker_id=self.worker_id,
            claims=self.claims,
            succeeded=self.jobs_completed,
            failed=self.jobs_failed,
        )

    async def drain(self, *, max_idle_rounds: int = 3, timeout_seconds: float = 60.0) -> None:
        """Process until the queue is idle for max_idle_rounds or timeout."""
        idle = 0
        deadline = time.monotonic() + timeout_seconds
        while idle < max_idle_rounds and time.monotonic() < deadline and not self._stop:
            await self.recover_stale()
            did_work = await self.process_one()
            if did_work:
                idle = 0
            else:
                idle += 1
                await asyncio.sleep(self.settings.evaluation_poll_interval_seconds)


async def run_worker(settings: Settings | None = None, worker_id: str | None = None) -> None:
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)
    init_engine(cfg)
    worker = EvaluationWorker(cfg, worker_id=worker_id)
    try:
        await worker.run_forever()
    finally:
        await dispose_engine()
