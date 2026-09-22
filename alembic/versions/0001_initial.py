"""Initial ChallengeForge schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('organizer', 'participant')", name="ck_users_role"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "hackathons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("organizer_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'closed')", name="ck_hackathons_status"
        ),
        sa.ForeignKeyConstraint(["organizer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hackathons_organizer_id", "hackathons", ["organizer_id"])

    op.create_table(
        "challenges",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("hackathon_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("constraints", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'published', 'closed')", name="ck_challenges_status"
        ),
        sa.ForeignKeyConstraint(["hackathon_id"], ["hackathons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_challenges_hackathon_id", "challenges", ["hackathon_id"])

    op.create_table(
        "challenge_specifications",
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("challenge_id"),
    )

    op.create_table(
        "evaluation_criteria",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.CheckConstraint("weight >= 1", name="ck_evaluation_criteria_weight"),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_evaluation_criteria_challenge_id", "evaluation_criteria", ["challenge_id"]
    )

    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("challenge_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("artifact_key", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('created', 'submitted', 'cancelled', 'failed')",
            name="ck_submissions_status",
        ),
        sa.ForeignKeyConstraint(["challenge_id"], ["challenges.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["participant_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_submissions_participant_created",
        "submissions",
        ["participant_id", "created_at"],
    )
    op.create_index(
        "ix_submissions_challenge_created",
        "submissions",
        ["challenge_id", "created_at"],
    )
    op.create_index(
        "uq_submissions_participant_idempotency",
        "submissions",
        ["participant_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_submissions_participant_idempotency", table_name="submissions")
    op.drop_index("ix_submissions_challenge_created", table_name="submissions")
    op.drop_index("ix_submissions_participant_created", table_name="submissions")
    op.drop_table("submissions")
    op.drop_index("ix_evaluation_criteria_challenge_id", table_name="evaluation_criteria")
    op.drop_table("evaluation_criteria")
    op.drop_table("challenge_specifications")
    op.drop_index("ix_challenges_hackathon_id", table_name="challenges")
    op.drop_table("challenges")
    op.drop_index("ix_hackathons_organizer_id", table_name="hackathons")
    op.drop_table("hackathons")
    op.drop_table("users")
