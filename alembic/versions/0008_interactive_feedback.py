"""Publish interactive p95 hint on scheduler singleton for workers.

Revision ID: 0008_interactive_feedback
Revises: 0007_adaptive_evaluation_v2
Create Date: 2026-09-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_interactive_feedback"
down_revision: Union[str, Sequence[str], None] = "0007_adaptive_evaluation_v2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_scheduler_state",
        sa.Column("interactive_p95_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "evaluation_scheduler_state",
        sa.Column(
            "interactive_sample_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("evaluation_scheduler_state", "interactive_sample_count")
    op.drop_column("evaluation_scheduler_state", "interactive_p95_ms")
