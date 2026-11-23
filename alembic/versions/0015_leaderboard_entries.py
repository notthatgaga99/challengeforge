"""Create leaderboard_entries snapshot table.

Revision ID: 0015_leaderboard_entries
Revises: 0014_chunk_embeddings
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_leaderboard_entries"
down_revision: Union[str, Sequence[str], None] = "0014_chunk_embeddings"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leaderboard_entries",
        sa.Column(
            "challenge_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("challenges.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "hackathon_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("hackathons.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "participant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "evaluation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evaluations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "score >= 0 AND score <= 100",
            name="ck_leaderboard_entries_score",
        ),
    )
    op.create_index(
        "ix_leaderboard_entries_rank",
        "leaderboard_entries",
        ["challenge_id", "score", "completed_at", "submission_id"],
    )
    op.create_index(
        "ix_leaderboard_entries_hackathon",
        "leaderboard_entries",
        ["hackathon_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_leaderboard_entries_hackathon", table_name="leaderboard_entries")
    op.drop_index("ix_leaderboard_entries_rank", table_name="leaderboard_entries")
    op.drop_table("leaderboard_entries")
