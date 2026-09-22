from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('organizer', 'participant')", name="ck_users_role"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HackathonRow(Base):
    __tablename__ = "hackathons"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'closed')", name="ck_hackathons_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    organizer_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    challenges: Mapped[list[ChallengeRow]] = relationship(back_populates="hackathon")


class ChallengeRow(Base):
    __tablename__ = "challenges"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'closed')", name="ck_challenges_status"
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    hackathon_id: Mapped[UUID] = mapped_column(
        ForeignKey("hackathons.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    constraints: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    hackathon: Mapped[HackathonRow] = relationship(back_populates="challenges")
    specification: Mapped[ChallengeSpecificationRow] = relationship(
        back_populates="challenge", uselist=False, cascade="all, delete-orphan"
    )
    evaluation_criteria: Mapped[list[EvaluationCriterionRow]] = relationship(
        back_populates="challenge", cascade="all, delete-orphan"
    )


class ChallengeSpecificationRow(Base):
    __tablename__ = "challenge_specifications"

    challenge_id: Mapped[UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="CASCADE"), primary_key=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    challenge: Mapped[ChallengeRow] = relationship(back_populates="specification")


class EvaluationCriterionRow(Base):
    __tablename__ = "evaluation_criteria"
    __table_args__ = (
        CheckConstraint("weight >= 1", name="ck_evaluation_criteria_weight"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    challenge_id: Mapped[UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    challenge: Mapped[ChallengeRow] = relationship(back_populates="evaluation_criteria")


class SubmissionRow(Base):
    __tablename__ = "submissions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created', 'submitted', 'cancelled', 'failed')",
            name="ck_submissions_status",
        ),
        Index("ix_submissions_participant_created", "participant_id", "created_at"),
        Index("ix_submissions_challenge_created", "challenge_id", "created_at"),
        Index(
            "uq_submissions_participant_idempotency",
            "participant_id",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
        CheckConstraint(
            "("
            "idempotency_key IS NULL AND request_fingerprint IS NULL"
            ") OR ("
            "idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL"
            ")",
            name="ck_submissions_idempotency_fingerprint",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    challenge_id: Mapped[UUID] = mapped_column(
        ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False
    )
    participant_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    artifact_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    evaluation: Mapped[EvaluationRow | None] = relationship(
        back_populates="submission", uselist=False
    )


class EvaluationRow(Base):
    """One evaluation job per submission (unique submission_id)."""

    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_evaluations_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_evaluations_attempt_count"),
        CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="ck_evaluations_score",
        ),
        CheckConstraint(
            "workload_class IN ('light', 'medium', 'heavy')",
            name="ck_evaluations_workload_class",
        ),
        Index("ix_evaluations_status_created", "status", "created_at"),
        Index("ix_evaluations_running_started", "status", "started_at"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    submission_id: Mapped[UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(Text, nullable=False)
    workload_class: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'light'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    worker_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    submission: Mapped[SubmissionRow] = relationship(back_populates="evaluation")


class EvaluationSchedulerStateRow(Base):
    """Singleton row serializing the deliberately small claiming policy."""

    __tablename__ = "evaluation_scheduler_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_evaluation_scheduler_state_singleton"),
        CheckConstraint(
            "light_bypass_count >= 0",
            name="ck_evaluation_scheduler_state_bypass_count",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blocked_heavy_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("evaluations.id", ondelete="SET NULL"), nullable=True
    )
    light_bypass_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    pressure_state: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'normal'")
    )
    adaptive_max_workers: Mapped[int | None] = mapped_column(Integer, nullable=True)
