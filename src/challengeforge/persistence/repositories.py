from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from challengeforge.domain.enums import ChallengeStatus, HackathonStatus
from challengeforge.domain.models import Challenge, Evaluation, Hackathon, Submission, User
from challengeforge.persistence.mapping import (
    challenge_to_domain,
    evaluation_to_domain,
    hackathon_to_domain,
    submission_to_domain,
    user_to_domain,
)
from challengeforge.persistence.models import (
    ChallengeRow,
    ChallengeSpecificationRow,
    EvaluationCriterionRow,
    EvaluationRow,
    EvaluationSchedulerStateRow,
    HackathonRow,
    SubmissionRow,
    UserRow,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, user_id: UUID) -> User | None:
        row = await self.session.get(UserRow, user_id)
        return user_to_domain(row) if row else None

    async def list_all(self) -> list[User]:
        result = await self.session.execute(select(UserRow).order_by(UserRow.display_name))
        return [user_to_domain(row) for row in result.scalars().all()]


class HackathonRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, hackathon_id: UUID) -> Hackathon | None:
        row = await self.session.get(HackathonRow, hackathon_id)
        return hackathon_to_domain(row) if row else None

    async def list_visible(self, *, include_unpublished: bool) -> list[Hackathon]:
        stmt = select(HackathonRow).order_by(HackathonRow.created_at.desc())
        if not include_unpublished:
            stmt = stmt.where(HackathonRow.status == HackathonStatus.PUBLISHED.value)
        result = await self.session.execute(stmt)
        return [hackathon_to_domain(row) for row in result.scalars().all()]

    async def add(
        self, *, title: str, description: str, organizer_id: UUID
    ) -> Hackathon:
        now = utcnow()
        row = HackathonRow(
            id=uuid4(),
            title=title,
            description=description,
            status=HackathonStatus.DRAFT.value,
            organizer_id=organizer_id,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return hackathon_to_domain(row)

    async def save_status(self, hackathon_id: UUID, status: HackathonStatus) -> Hackathon:
        row = await self.session.get(HackathonRow, hackathon_id)
        assert row is not None
        row.status = status.value
        row.updated_at = utcnow()
        await self.session.flush()
        return hackathon_to_domain(row)


class ChallengeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _loaded(self):
        return selectinload(ChallengeRow.specification), selectinload(
            ChallengeRow.evaluation_criteria
        )

    async def get(self, challenge_id: UUID) -> Challenge | None:
        stmt = (
            select(ChallengeRow)
            .options(*self._loaded())
            .where(ChallengeRow.id == challenge_id)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return challenge_to_domain(row) if row else None

    async def get_for_submission_acceptance(
        self, challenge_id: UUID
    ) -> Challenge | None:
        """Hold a shared challenge-row lock through acceptance commit.

        Multiple submissions may share this lock. Challenge close takes the
        conflicting exclusive lock, making their commit order authoritative.
        """
        stmt = (
            select(ChallengeRow)
            .options(*self._loaded())
            .where(ChallengeRow.id == challenge_id)
            .with_for_update(read=True)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return challenge_to_domain(row) if row else None

    async def get_for_close(self, challenge_id: UUID) -> Challenge | None:
        """Hold an exclusive challenge-row lock through close commit."""
        stmt = (
            select(ChallengeRow)
            .options(*self._loaded())
            .where(ChallengeRow.id == challenge_id)
            .with_for_update()
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return challenge_to_domain(row) if row else None

    async def get_row(self, challenge_id: UUID) -> ChallengeRow | None:
        stmt = (
            select(ChallengeRow)
            .options(*self._loaded())
            .where(ChallengeRow.id == challenge_id)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_for_hackathon(
        self, hackathon_id: UUID, *, include_unpublished: bool
    ) -> list[Challenge]:
        stmt = (
            select(ChallengeRow)
            .options(*self._loaded())
            .where(ChallengeRow.hackathon_id == hackathon_id)
            .order_by(ChallengeRow.created_at.desc())
        )
        if not include_unpublished:
            stmt = stmt.where(ChallengeRow.status == ChallengeStatus.PUBLISHED.value)
        result = await self.session.execute(stmt)
        return [challenge_to_domain(row) for row in result.scalars().all()]

    async def add(
        self,
        *,
        hackathon_id: UUID,
        title: str,
        description: str,
        constraints: str,
        specification_body: str,
        criteria: list[tuple[str, str, int]],
    ) -> Challenge:
        now = utcnow()
        row = ChallengeRow(
            id=uuid4(),
            hackathon_id=hackathon_id,
            title=title,
            description=description,
            constraints=constraints,
            status=ChallengeStatus.DRAFT.value,
            created_at=now,
            updated_at=now,
            specification=ChallengeSpecificationRow(
                body=specification_body,
                created_at=now,
                updated_at=now,
            ),
            evaluation_criteria=[
                EvaluationCriterionRow(
                    id=uuid4(),
                    name=name,
                    description=description_,
                    weight=weight,
                )
                for name, description_, weight in criteria
            ],
        )
        self.session.add(row)
        await self.session.flush()
        return challenge_to_domain(row)

    async def save_status(self, challenge_id: UUID, status: ChallengeStatus) -> Challenge:
        row = await self.get_row(challenge_id)
        assert row is not None
        row.status = status.value
        row.updated_at = utcnow()
        await self.session.flush()
        return challenge_to_domain(row)


class SubmissionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, submission_id: UUID) -> Submission | None:
        row = await self.session.get(SubmissionRow, submission_id)
        return submission_to_domain(row) if row else None

    async def get_row(self, submission_id: UUID) -> SubmissionRow | None:
        return await self.session.get(SubmissionRow, submission_id)

    async def find_by_idempotency(
        self, participant_id: UUID, idempotency_key: str
    ) -> Submission | None:
        stmt = select(SubmissionRow).where(
            SubmissionRow.participant_id == participant_id,
            SubmissionRow.idempotency_key == idempotency_key,
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return submission_to_domain(row) if row else None

    async def list_for_participant(self, participant_id: UUID) -> list[Submission]:
        stmt = (
            select(SubmissionRow)
            .where(SubmissionRow.participant_id == participant_id)
            .order_by(SubmissionRow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return [submission_to_domain(row) for row in result.scalars().all()]

    async def list_for_challenge(self, challenge_id: UUID) -> list[Submission]:
        stmt = (
            select(SubmissionRow)
            .where(SubmissionRow.challenge_id == challenge_id)
            .order_by(SubmissionRow.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return [submission_to_domain(row) for row in result.scalars().all()]

    async def list_artifact_keys(self) -> list[str]:
        """All durable artifact keys (for orphan reconciliation)."""
        stmt = select(SubmissionRow.artifact_key).where(
            SubmissionRow.artifact_key.is_not(None)
        )
        result = await self.session.execute(stmt)
        return [key for key in result.scalars().all() if key]

    async def add(
        self,
        *,
        challenge_id: UUID,
        participant_id: UUID,
        metadata: dict,
        artifact_key: str | None,
        idempotency_key: str | None,
        request_fingerprint: str | None,
    ) -> Submission:
        now = utcnow()
        row = SubmissionRow(
            id=uuid4(),
            challenge_id=challenge_id,
            participant_id=participant_id,
            status="created",
            metadata_json=metadata,
            artifact_key=artifact_key,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            created_at=now,
            updated_at=now,
        )
        self.session.add(row)
        await self.session.flush()
        return submission_to_domain(row)

    async def transition_if_created(
        self,
        *,
        submission_id: UUID,
        participant_id: UUID,
        target_status: str,
        updated_at: datetime,
    ) -> Submission | None:
        """Atomically transition one owned CREATED row and return the winner."""
        stmt = (
            update(SubmissionRow)
            .where(
                SubmissionRow.id == submission_id,
                SubmissionRow.participant_id == participant_id,
                SubmissionRow.status == "created",
            )
            .values(status=target_status, updated_at=updated_at)
            .returning(SubmissionRow)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return submission_to_domain(row) if row else None


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, evaluation_id: UUID) -> Evaluation | None:
        row = await self.session.get(EvaluationRow, evaluation_id)
        return evaluation_to_domain(row) if row else None

    async def get_by_submission(self, submission_id: UUID) -> Evaluation | None:
        stmt = select(EvaluationRow).where(EvaluationRow.submission_id == submission_id)
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return evaluation_to_domain(row) if row else None

    async def create_queued(
        self,
        submission_id: UUID,
        *,
        workload_class: str = "light",
        evaluation_mode: str = "legacy",
        deadline_at: datetime | None = None,
        current_stage: int = 0,
    ) -> Evaluation:
        now = utcnow()
        row = EvaluationRow(
            id=uuid4(),
            submission_id=submission_id,
            status="queued",
            workload_class=workload_class,
            current_stage=current_stage,
            evaluation_mode=evaluation_mode,
            deadline_at=deadline_at,
            created_at=now,
            started_at=None,
            completed_at=None,
            attempt_count=0,
            failure_reason=None,
            score=None,
            result_metadata={},
            worker_id=None,
        )
        self.session.add(row)
        await self.session.flush()
        return evaluation_to_domain(row)

    async def defer_escalation(
        self,
        evaluation_id: UUID,
        *,
        worker_id: str,
        next_stage: int,
        workload_class: str,
        result_metadata: dict,
    ) -> Evaluation | None:
        """Release RUNNING → QUEUED after a partial progressive stage."""
        stmt = (
            update(EvaluationRow)
            .where(
                EvaluationRow.id == evaluation_id,
                EvaluationRow.status == "running",
                EvaluationRow.worker_id == worker_id,
            )
            .values(
                status="queued",
                started_at=None,
                worker_id=None,
                current_stage=next_stage,
                workload_class=workload_class,
                result_metadata=result_metadata,
                failure_reason=None,
            )
            .returning(EvaluationRow)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return evaluation_to_domain(row) if row else None

    async def release_for_retry(
        self,
        evaluation_id: UUID,
        *,
        worker_id: str,
        result_metadata: dict,
        failure_reason: str,
    ) -> Evaluation | None:
        """Release RUNNING → QUEUED after retryable infrastructure execution failure."""
        stmt = (
            update(EvaluationRow)
            .where(
                EvaluationRow.id == evaluation_id,
                EvaluationRow.status == "running",
                EvaluationRow.worker_id == worker_id,
            )
            .values(
                status="queued",
                started_at=None,
                worker_id=None,
                result_metadata=result_metadata,
                failure_reason=failure_reason,
            )
            .returning(EvaluationRow)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return evaluation_to_domain(row) if row else None

    async def claim_next(
        self,
        *,
        worker_id: str,
        scheduling_policy: str = "bounded_light_bypass",
        max_workers: int = 2,
        max_concurrent_heavy: int = 1,
        light_bypass_limit: int = 2,
    ) -> Evaluation | None:
        """Claim one job under FIFO plus a narrowly bounded LIGHT bypass.

        A singleton PostgreSQL row serializes the short claim decision. This
        keeps the global concurrency budget and bypass counter correct across
        worker processes. Evaluation work still happens after this transaction.

        "Blocked HEAVY" means the oldest queued row is HEAVY while the configured
        HEAVY concurrency slot is already occupied. Only the consecutive LIGHT
        row directly behind it may bypass, and only ``light_bypass_limit`` times.
        """
        max_workers = max(1, max_workers)
        # 0 is intentional under DEGRADED (block new HEAVY; allow LIGHT bypass).
        max_concurrent_heavy = max(0, min(max_concurrent_heavy, max_workers))
        light_bypass_limit = max(0, light_bypass_limit)
        # resource_aware uses the same claim mechanics as bounded LIGHT bypass;
        # expensive-plane admission / pressure live outside this transaction.
        if scheduling_policy == "resource_aware":
            scheduling_policy = "bounded_light_bypass"

        await self.session.execute(
            pg_insert(EvaluationSchedulerStateRow)
            .values(
                id=1,
                blocked_heavy_id=None,
                light_bypass_count=0,
                pressure_state="normal",
                adaptive_max_workers=None,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        state = (
            await self.session.execute(
                select(EvaluationSchedulerStateRow)
                .where(EvaluationSchedulerStateRow.id == 1)
                .with_for_update()
            )
        ).scalar_one()

        running = (
            await self.session.execute(
                select(func.count())
                .select_from(EvaluationRow)
                .where(EvaluationRow.status == "running")
            )
        ).scalar_one()
        if int(running) >= max_workers:
            return None

        oldest = (
            await self.session.execute(
                select(EvaluationRow)
                .where(EvaluationRow.status == "queued")
                .order_by(EvaluationRow.created_at.asc(), EvaluationRow.id.asc())
                .with_for_update()
                .limit(1)
            )
        ).scalar_one_or_none()
        if oldest is None:
            state.blocked_heavy_id = None
            state.light_bypass_count = 0
            return None

        row = oldest
        bounded_policy = scheduling_policy == "bounded_light_bypass"
        if oldest.workload_class == "heavy":
            active_heavy = (
                await self.session.execute(
                    select(func.count())
                    .select_from(EvaluationRow)
                    .where(
                        EvaluationRow.status == "running",
                        EvaluationRow.workload_class == "heavy",
                    )
                )
            ).scalar_one()
            heavy_is_blocked = int(active_heavy) >= max_concurrent_heavy
            if heavy_is_blocked or max_concurrent_heavy == 0:
                if not bounded_policy:
                    return None
                if state.blocked_heavy_id != oldest.id:
                    state.blocked_heavy_id = oldest.id
                    state.light_bypass_count = 0
                if state.light_bypass_count >= light_bypass_limit:
                    return None

                # Deliberately inspect only the immediate FIFO successor. This
                # prevents LIGHT from becoming an arbitrary priority class.
                successor = (
                    await self.session.execute(
                        select(EvaluationRow)
                        .where(EvaluationRow.status == "queued")
                        .order_by(
                            EvaluationRow.created_at.asc(), EvaluationRow.id.asc()
                        )
                        .offset(1)
                        .with_for_update()
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if successor is None or successor.workload_class != "light":
                    return None
                row = successor
                state.light_bypass_count += 1
            else:
                state.blocked_heavy_id = None
                state.light_bypass_count = 0
        else:
            state.blocked_heavy_id = None
            state.light_bypass_count = 0

        if row is None:
            return None
        now = utcnow()
        row.status = "running"
        row.started_at = now
        row.attempt_count = row.attempt_count + 1
        row.worker_id = worker_id
        row.failure_reason = None
        await self.session.flush()
        return evaluation_to_domain(row)

    async def persist_runtime_state(
        self, *, pressure_state: str, adaptive_max_workers: int
    ) -> None:
        """Publish worker pressure / concurrency for API observability."""
        await self.session.execute(
            pg_insert(EvaluationSchedulerStateRow)
            .values(
                id=1,
                blocked_heavy_id=None,
                light_bypass_count=0,
                pressure_state=pressure_state,
                adaptive_max_workers=adaptive_max_workers,
                interactive_sample_count=0,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        state = (
            await self.session.execute(
                select(EvaluationSchedulerStateRow)
                .where(EvaluationSchedulerStateRow.id == 1)
                .with_for_update()
            )
        ).scalar_one()
        state.pressure_state = pressure_state
        state.adaptive_max_workers = adaptive_max_workers
        await self.session.flush()

    async def persist_interactive_hint(
        self,
        *,
        interactive_p95_ms: float | None,
        sample_count: int,
        pool_wait_p95_ms: float | None = None,
    ) -> None:
        """API publishes rolling interactive hints for workers (cross-process)."""
        await self.session.execute(
            pg_insert(EvaluationSchedulerStateRow)
            .values(
                id=1,
                blocked_heavy_id=None,
                light_bypass_count=0,
                pressure_state="normal",
                adaptive_max_workers=None,
                interactive_p95_ms=interactive_p95_ms,
                interactive_sample_count=max(0, int(sample_count)),
                pool_wait_p95_ms=pool_wait_p95_ms,
            )
            .on_conflict_do_nothing(index_elements=["id"])
        )
        state = (
            await self.session.execute(
                select(EvaluationSchedulerStateRow)
                .where(EvaluationSchedulerStateRow.id == 1)
                .with_for_update()
            )
        ).scalar_one()
        state.interactive_p95_ms = interactive_p95_ms
        state.interactive_sample_count = max(0, int(sample_count))
        if hasattr(state, "pool_wait_p95_ms"):
            state.pool_wait_p95_ms = pool_wait_p95_ms
        await self.session.flush()

    async def read_runtime_state(self) -> dict:
        row = (
            await self.session.execute(
                select(EvaluationSchedulerStateRow).where(
                    EvaluationSchedulerStateRow.id == 1
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return {
                "pressure_state": "normal",
                "adaptive_max_workers": None,
                "interactive_p95_ms": None,
                "interactive_sample_count": 0,
                "pool_wait_p95_ms": None,
            }
        return {
            "pressure_state": row.pressure_state or "normal",
            "adaptive_max_workers": row.adaptive_max_workers,
            "interactive_p95_ms": row.interactive_p95_ms,
            "interactive_sample_count": int(
                getattr(row, "interactive_sample_count", 0) or 0
            ),
            "pool_wait_p95_ms": getattr(row, "pool_wait_p95_ms", None),
        }

    async def queue_position(self, evaluation_id: UUID) -> int | None:
        """Point-in-time count of queued jobs ahead in default FIFO order.

        Bounded bypass means this is not a guaranteed execution position.
        RUNNING evaluations are not included.
        """
        from challengeforge.request_profile import timed_stage

        with timed_stage("queue_position"):
            row = await self.session.get(EvaluationRow, evaluation_id)
            if row is None or row.status != "queued":
                return None
            ahead = (
                await self.session.execute(
                    select(func.count())
                    .select_from(EvaluationRow)
                    .where(
                        EvaluationRow.status == "queued",
                        (
                            (EvaluationRow.created_at < row.created_at)
                            | (
                                (EvaluationRow.created_at == row.created_at)
                                & (EvaluationRow.id < row.id)
                            )
                        ),
                    )
                )
            ).scalar_one()
            return int(ahead)

    async def list_queued_fifo(self, *, limit: int = 100) -> list[Evaluation]:
        stmt = (
            select(EvaluationRow)
            .where(EvaluationRow.status == "queued")
            .order_by(EvaluationRow.created_at.asc(), EvaluationRow.id.asc())
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return [evaluation_to_domain(row) for row in result.scalars().all()]

    async def mark_succeeded(
        self,
        evaluation_id: UUID,
        *,
        score: int,
        result_metadata: dict,
        worker_id: str,
    ) -> Evaluation | None:
        """Complete only if still RUNNING for this worker (prevents stale writes)."""
        now = utcnow()
        stmt = (
            update(EvaluationRow)
            .where(
                EvaluationRow.id == evaluation_id,
                EvaluationRow.status == "running",
                EvaluationRow.worker_id == worker_id,
            )
            .values(
                status="succeeded",
                completed_at=now,
                score=score,
                result_metadata=result_metadata,
                failure_reason=None,
            )
            .returning(EvaluationRow)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return evaluation_to_domain(row) if row else None

    async def mark_failed(
        self,
        evaluation_id: UUID,
        *,
        failure_reason: str,
        result_metadata: dict | None = None,
        worker_id: str,
    ) -> Evaluation | None:
        """Fail only if still RUNNING for this worker."""
        now = utcnow()
        values: dict = {
            "status": "failed",
            "completed_at": now,
            "failure_reason": failure_reason,
            "score": None,
        }
        if result_metadata is not None:
            values["result_metadata"] = result_metadata
        stmt = (
            update(EvaluationRow)
            .where(
                EvaluationRow.id == evaluation_id,
                EvaluationRow.status == "running",
                EvaluationRow.worker_id == worker_id,
            )
            .values(**values)
            .returning(EvaluationRow)
        )
        result = await self.session.execute(stmt)
        row = result.scalar_one_or_none()
        return evaluation_to_domain(row) if row else None

    async def begin_execution_attempt(
        self,
        evaluation_id: UUID,
        *,
        worker_id: str,
        execution_attempt: dict,
    ) -> Evaluation | None:
        """Persist OS ownership metadata while RUNNING (duplicate-prevention)."""
        row = (
            await self.session.execute(
                select(EvaluationRow)
                .where(
                    EvaluationRow.id == evaluation_id,
                    EvaluationRow.status == "running",
                    EvaluationRow.worker_id == worker_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        meta = dict(row.result_metadata or {})
        meta["execution_attempt"] = execution_attempt
        meta["evaluator"] = meta.get("evaluator") or "synthetic_execution_v1"
        row.result_metadata = meta
        await self.session.flush()
        return evaluation_to_domain(row)

    async def recover_stale_running(
        self,
        *,
        stale_before: datetime,
        max_attempts: int,
    ) -> list[Evaluation]:
        """Contain orphaned executions, then requeue or fail stale RUNNING rows.

        Never requeues while a prior execution_attempt root_pid is still alive
        without first attempting containment.
        """
        from challengeforge.execution.ownership import (
            ExecutionAttempt,
            contain_attempt,
            orphan_suspected,
        )

        recovered: list[Evaluation] = []

        stale_rows = (
            await self.session.execute(
                select(EvaluationRow)
                .where(
                    EvaluationRow.status == "running",
                    EvaluationRow.started_at.is_not(None),
                    EvaluationRow.started_at < stale_before,
                )
                .with_for_update()
            )
        ).scalars().all()

        for row in stale_rows:
            meta = dict(row.result_metadata or {})
            attempt = ExecutionAttempt.from_metadata(meta)
            containment = None
            if attempt is not None and orphan_suspected(attempt):
                containment = contain_attempt(attempt)
                meta["ownership_recovery"] = containment
                # If still alive after containment, quarantine — do not requeue.
                if containment.get("still_alive"):
                    meta["orphan_suspected"] = True
                    row.result_metadata = meta
                    row.status = "failed"
                    row.completed_at = utcnow()
                    row.failure_reason = (
                        "orphan_suspected: prior execution still alive after "
                        "containment; refused duplicate requeue"
                    )
                    row.worker_id = None
                    recovered.append(evaluation_to_domain(row))
                    continue
                meta["orphan_suspected"] = False
            elif attempt is not None:
                # Record that we checked and found no live process.
                meta["ownership_recovery"] = {
                    "execution_attempt_id": attempt.execution_attempt_id,
                    "root_pid": attempt.root_pid,
                    "was_alive": False,
                    "contained": True,
                }

            row.result_metadata = meta
            if row.attempt_count >= max_attempts:
                row.status = "failed"
                row.completed_at = utcnow()
                row.failure_reason = (
                    f"Abandoned after {max_attempts} attempt(s) without completion "
                    "(worker crash / stale RUNNING)."
                )
                row.worker_id = None
            else:
                row.status = "queued"
                row.started_at = None
                row.worker_id = None
                row.failure_reason = (
                    "Requeued after stale RUNNING (worker crash recovery; "
                    "prior execution contained)."
                )
            recovered.append(evaluation_to_domain(row))

        await self.session.flush()
        return recovered

    async def plane_snapshot(self) -> dict:
        """Cheap counts for the resource-aware runtime control loop."""
        from sqlalchemy import func

        counts = await self.count_by_status()
        running_heavy = (
            await self.session.execute(
                select(func.count())
                .select_from(EvaluationRow)
                .where(
                    EvaluationRow.status == "running",
                    EvaluationRow.workload_class == "heavy",
                )
            )
        ).scalar_one()
        oldest_class = (
            await self.session.execute(
                select(EvaluationRow.workload_class)
                .where(EvaluationRow.status == "queued")
                .order_by(EvaluationRow.created_at.asc(), EvaluationRow.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return {
            "queued": int(counts.get("queued", 0)),
            "running": int(counts.get("running", 0)),
            "running_heavy": int(running_heavy),
            "oldest_workload_class": oldest_class,
        }

    async def count_by_status(self) -> dict[str, int]:
        from sqlalchemy import func

        stmt = select(EvaluationRow.status, func.count()).group_by(EvaluationRow.status)
        result = await self.session.execute(stmt)
        return {status: count for status, count in result.all()}

    async def queue_snapshot(self, *, service_rate_window_seconds: float = 30.0) -> dict:
        """Counts, oldest queued age, and recent completion rate for backlog health."""
        counts = await self.count_by_status()
        queued_by_class = {
            workload_class: int(count)
            for workload_class, count in (
                await self.session.execute(
                    select(EvaluationRow.workload_class, func.count())
                    .where(EvaluationRow.status == "queued")
                    .group_by(EvaluationRow.workload_class)
                )
            ).all()
        }
        oldest = (
            await self.session.execute(
                select(func.min(EvaluationRow.created_at)).where(
                    EvaluationRow.status == "queued"
                )
            )
        ).scalar_one_or_none()
        oldest_age_seconds = None
        if oldest is not None:
            oldest_age_seconds = max(0.0, (utcnow() - oldest).total_seconds())

        window_start = utcnow() - timedelta(seconds=service_rate_window_seconds)
        recent_completed = (
            await self.session.execute(
                select(func.count())
                .select_from(EvaluationRow)
                .where(
                    EvaluationRow.status.in_(("succeeded", "failed")),
                    EvaluationRow.completed_at.is_not(None),
                    EvaluationRow.completed_at >= window_start,
                )
            )
        ).scalar_one()
        return {
            "queued": int(counts.get("queued", 0)),
            "running": int(counts.get("running", 0)),
            "succeeded": int(counts.get("succeeded", 0)),
            "failed": int(counts.get("failed", 0)),
            "oldest_queued_at": oldest,
            "oldest_queue_age_seconds": oldest_age_seconds,
            "recent_completed_count": int(recent_completed),
            "light_queued": queued_by_class.get("light", 0),
            "medium_queued": queued_by_class.get("medium", 0),
            "heavy_queued": queued_by_class.get("heavy", 0),
        }

    async def count_queued_by_challenge(self) -> dict[UUID, int]:
        from sqlalchemy import func

        stmt = (
            select(SubmissionRow.challenge_id, func.count())
            .select_from(EvaluationRow)
            .join(SubmissionRow, SubmissionRow.id == EvaluationRow.submission_id)
            .where(EvaluationRow.status == "queued")
            .group_by(SubmissionRow.challenge_id)
        )
        result = await self.session.execute(stmt)
        return {challenge_id: int(count) for challenge_id, count in result.all()}

    async def list_all(self) -> list[Evaluation]:
        stmt = select(EvaluationRow).order_by(EvaluationRow.created_at.asc())
        result = await self.session.execute(stmt)
        return [evaluation_to_domain(row) for row in result.scalars().all()]
