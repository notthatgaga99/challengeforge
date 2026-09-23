"""Add pool_wait_p95_ms to interactive hint publication.

Revision ID: 0009_interactive_hint_pool_wait
Revises: 0008_interactive_feedback
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_interactive_hint_pool_wait"
down_revision: Union[str, Sequence[str], None] = "0008_interactive_feedback"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_scheduler_state",
        sa.Column("pool_wait_p95_ms", sa.Float(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_scheduler_state", "pool_wait_p95_ms")
