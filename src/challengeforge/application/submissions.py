from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError

from challengeforge.application.evaluator import parse_workload_class
from challengeforge.application import UnitOfWork, require_participant
from challengeforge.application.artifacts import compensate_delete
from challengeforge.application.idempotency import submission_request_fingerprint
from challengeforge.domain.enums import ChallengeStatus, HackathonStatus, SubmissionStatus
from challengeforge.domain.exceptions import (
    ConflictError,
    InvalidStateTransition,
    NotFoundError,
    PermissionDenied,
    ValidationFailed,
)
from challengeforge.domain.models import Evaluation, Submission
from challengeforge.identity import CurrentUser
from challengeforge.persistence.mapping import utcnow


class SubmissionCreateResult:
    def __init__(self, submission: Submission, replayed: bool) -> None:
        self.submission = submission
        self.replayed = replayed


class SubmissionSubmitResult:
    """Submission accepted; evaluation is queued asynchronously (not scored yet)."""

    def __init__(self, submission: Submission, evaluation: Evaluation) -> None:
        self.submission = submission
        self.evaluation = evaluation


class SubmissionService:
    def __init__(self, uow: UnitOfWork) -> None:
        self.uow = uow

    def _validate_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        payload = metadata or {}
        if not isinstance(payload, dict):
            raise ValidationFailed("Submission metadata must be a JSON object.")
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > self.uow.settings.metadata_max_bytes:
            raise ValidationFailed("Submission metadata exceeds the size limit.")
        return payload

    async def _lock_open_challenge(self, challenge_id: UUID):
        challenge = await self.uow.challenges.get_for_submission_acceptance(
            challenge_id
        )
        if challenge is None:
            raise NotFoundError("Challenge not found.")
        if challenge.status == ChallengeStatus.CLOSED:
            raise ConflictError("Challenge is closed and no longer accepts submissions.")
        hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
        if (
            hackathon is None
            or hackathon.status != HackathonStatus.PUBLISHED
            or challenge.status != ChallengeStatus.PUBLISHED
        ):
            raise NotFoundError("Challenge not found.")
        return challenge

    def _replay_or_conflict(
        self, existing: Submission, request_fingerprint: str
    ) -> SubmissionCreateResult:
        stored_fingerprint = (
            existing.request_fingerprint
            or submission_request_fingerprint(
                existing.challenge_id, existing.metadata
            )
        )
        if stored_fingerprint != request_fingerprint:
            raise ConflictError(
                "Idempotency-Key is already associated with a different "
                "submission request."
            )
        return SubmissionCreateResult(existing, replayed=True)

    async def create(
        self,
        actor: CurrentUser,
        challenge_id: UUID,
        *,
        metadata: dict[str, Any] | None,
        idempotency_key: str | None,
        artifact: bytes | None = None,
        artifact_content_type: str | None = None,
        artifact_filename: str | None = None,
    ) -> SubmissionCreateResult:
        require_participant(actor)
        payload = self._validate_metadata(metadata)
        key = (idempotency_key or "").strip() or None
        request_fingerprint = (
            submission_request_fingerprint(challenge_id, payload) if key else None
        )

        if key:
            existing = await self.uow.submissions.find_by_idempotency(actor.id, key)
            if existing is not None:
                assert request_fingerprint is not None
                return self._replay_or_conflict(existing, request_fingerprint)

        # The shared row lock is held through INSERT + COMMIT. Challenge close
        # takes the conflicting exclusive lock, so one operation wins first.
        await self._lock_open_challenge(challenge_id)

        artifact_key = None
        if artifact is not None:
            if len(artifact) > self.uow.settings.artifact_max_bytes:
                raise ValidationFailed("Artifact exceeds the size limit.")
            suffix = ""
            if artifact_filename and "." in artifact_filename:
                suffix = "." + artifact_filename.rsplit(".", 1)[-1].lower()
            artifact_key = f"submissions/{challenge_id}/{actor.id}/{uuid4()}{suffix}"
            self.uow.storage.put(
                artifact_key, artifact, artifact_content_type or "application/octet-stream"
            )

        try:
            submission = await self.uow.submissions.add(
                challenge_id=challenge_id,
                participant_id=actor.id,
                metadata=payload,
                artifact_key=artifact_key,
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
            )
            await self.uow.session.commit()
        except IntegrityError:
            await self.uow.session.rollback()
            compensate_delete(self.uow.storage, artifact_key)
            if key:
                existing = await self.uow.submissions.find_by_idempotency(actor.id, key)
                if existing is not None:
                    assert request_fingerprint is not None
                    return self._replay_or_conflict(existing, request_fingerprint)
            raise
        except Exception:
            await self.uow.session.rollback()
            compensate_delete(self.uow.storage, artifact_key)
            raise
        return SubmissionCreateResult(submission, replayed=False)

    async def get(self, actor: CurrentUser, submission_id: UUID) -> Submission:
        submission = await self.uow.submissions.get(submission_id)
        if submission is None:
            raise NotFoundError("Submission not found.")
        if submission.participant_id != actor.id and not actor.is_organizer:
            raise PermissionDenied("You cannot view this submission.")
        if actor.is_organizer and submission.participant_id != actor.id:
            challenge = await self.uow.challenges.get(submission.challenge_id)
            if challenge is None:
                raise NotFoundError("Submission not found.")
            hackathon = await self.uow.hackathons.get(challenge.hackathon_id)
            if hackathon is None or hackathon.organizer_id != actor.id:
                raise PermissionDenied("You cannot view this submission.")
        return submission

    async def list_for_user(self, actor: CurrentUser, user_id: UUID) -> list[Submission]:
        if actor.id != user_id and not actor.is_organizer:
            raise PermissionDenied("You cannot list another participant's submissions.")
        target = await self.uow.users.get(user_id)
        if target is None:
            raise NotFoundError("User not found.")
        return await self.uow.submissions.list_for_participant(user_id)

    async def update_created(
        self,
        actor: CurrentUser,
        submission_id: UUID,
        *,
        metadata: dict[str, Any] | None = None,
        artifact: bytes | None = None,
        artifact_content_type: str | None = None,
        artifact_filename: str | None = None,
    ) -> Submission:
        row = await self.uow.submissions.get_row(submission_id)
        if row is None:
            raise NotFoundError("Submission not found.")
        if row.participant_id != actor.id:
            raise PermissionDenied("You cannot modify this submission.")
        if row.status != SubmissionStatus.CREATED.value:
            raise ValidationFailed("Only submissions in CREATED can be updated.")
        previous_artifact_key = row.artifact_key
        new_artifact_key: str | None = None
        if metadata is not None:
            row.metadata_json = self._validate_metadata(metadata)
        if artifact is not None:
            if len(artifact) > self.uow.settings.artifact_max_bytes:
                raise ValidationFailed("Artifact exceeds the size limit.")
            suffix = ""
            if artifact_filename and "." in artifact_filename:
                suffix = "." + artifact_filename.rsplit(".", 1)[-1].lower()
            new_artifact_key = (
                f"submissions/{row.challenge_id}/{actor.id}/{uuid4()}{suffix}"
            )
            self.uow.storage.put(
                new_artifact_key,
                artifact,
                artifact_content_type or "application/octet-stream",
            )
            row.artifact_key = new_artifact_key
        row.updated_at = utcnow()
        try:
            await self.uow.session.commit()
        except Exception:
            await self.uow.session.rollback()
            compensate_delete(self.uow.storage, new_artifact_key)
            raise
        # Successful replace: drop the previous blob (best-effort).
        if (
            new_artifact_key is not None
            and previous_artifact_key
            and previous_artifact_key != new_artifact_key
        ):
            compensate_delete(self.uow.storage, previous_artifact_key)
        updated = await self.uow.submissions.get(submission_id)
        assert updated is not None
        return updated

    async def submit(self, actor: CurrentUser, submission_id: UUID) -> SubmissionSubmitResult:
        """CREATED → SUBMITTED and create QUEUED evaluation in one transaction.

        Atomicity boundary: conditional submission UPDATE + evaluation INSERT
        commit together. Neither half can persist alone.
        """
        require_participant(actor)
        transitioned = await self.uow.submissions.transition_if_created(
            submission_id=submission_id,
            participant_id=actor.id,
            target_status=SubmissionStatus.SUBMITTED.value,
            updated_at=utcnow(),
        )
        if transitioned is not None:
            workload = parse_workload_class(transitioned.metadata.get("workload_class"))
            mode = self.uow.settings.evaluation_progressive_mode
            # Opt-in per-submission override for experiments only.
            raw_mode = transitioned.metadata.get("evaluation_mode")
            if isinstance(raw_mode, str) and raw_mode in {
                "legacy",
                "always_expensive",
                "fixed_progressive",
                "resource_aware_adaptive",
                "synthetic_execution",
            }:
                mode = raw_mode
            deadline_at = None
            raw_deadline = transitioned.metadata.get("evaluation_deadline_seconds")
            if raw_deadline is not None:
                try:
                    from datetime import timedelta

                    from challengeforge.persistence.mapping import utcnow as _utcnow

                    deadline_at = _utcnow() + timedelta(seconds=float(raw_deadline))
                except (TypeError, ValueError):
                    deadline_at = None
            evaluation = await self.uow.evaluations.create_queued(
                transitioned.id,
                workload_class=workload.value,
                evaluation_mode=mode,
                deadline_at=deadline_at,
            )
            await self.uow.session.commit()
            return SubmissionSubmitResult(transitioned, evaluation)

        current = await self.uow.submissions.get(submission_id)
        if current is None:
            raise NotFoundError("Submission not found.")
        if current.participant_id != actor.id:
            raise PermissionDenied("You cannot change this submission.")
        if current.status == SubmissionStatus.SUBMITTED:
            # Lost-response retry: submission already accepted; return existing
            # evaluation without creating a second one (unique submission_id).
            evaluation = await self.uow.evaluations.get_by_submission(submission_id)
            if evaluation is None:
                raise ConflictError(
                    "Submission is submitted but has no evaluation; "
                    "this violates the product invariant."
                )
            return SubmissionSubmitResult(current, evaluation)
        raise InvalidStateTransition(
            f"Cannot transition submission from {current.status} to "
            f"{SubmissionStatus.SUBMITTED}."
        )

    async def cancel(self, actor: CurrentUser, submission_id: UUID) -> Submission:
        return await self._transition(actor, submission_id, SubmissionStatus.CANCELLED)

    async def _transition(
        self, actor: CurrentUser, submission_id: UUID, target: SubmissionStatus
    ) -> Submission:
        transitioned = await self.uow.submissions.transition_if_created(
            submission_id=submission_id,
            participant_id=actor.id,
            target_status=target.value,
            updated_at=utcnow(),
        )
        if transitioned is not None:
            await self.uow.session.commit()
            return transitioned

        current = await self.uow.submissions.get(submission_id)
        if current is None:
            raise NotFoundError("Submission not found.")
        if current.participant_id != actor.id:
            raise PermissionDenied("You cannot change this submission.")
        raise InvalidStateTransition(
            f"Cannot transition submission from {current.status} to {target}."
        )
