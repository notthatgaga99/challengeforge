"""Create evaluations table for asynchronous grading jobs.

Revision ID: 0003_evaluation_pipeline
Revises: 0002_concurrency_correctness
Create Date: 2026-09-21
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_evaluation_pipeline"
down_revision: Union[str, Sequence[str], None] = "0002_concurrency_correctness"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column(
            "result_metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_evaluations_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_evaluations_attempt_count"),
        sa.CheckConstraint(
            "score IS NULL OR (score >= 0 AND score <= 100)",
            name="ck_evaluations_score",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"], ["submissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id"),
    )
    op.create_index(
        "ix_evaluations_status_created",
        "evaluations",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_evaluations_running_started",
        "evaluations",
        ["status", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_evaluations_running_started", table_name="evaluations")
    op.drop_index("ix_evaluations_status_created", table_name="evaluations")
    op.drop_table("evaluations")
