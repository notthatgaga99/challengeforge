"""Add durable state for bounded LIGHT bypass scheduling.

Revision ID: 0005_bounded_light_bypass
Revises: 0004_evaluation_workload_class
Create Date: 2026-09-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_bounded_light_bypass"
down_revision: Union[str, Sequence[str], None] = "0004_evaluation_workload_class"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "evaluation_scheduler_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("blocked_heavy_id", sa.Uuid(), nullable=True),
        sa.Column(
            "light_bypass_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.CheckConstraint(
            "id = 1", name="ck_evaluation_scheduler_state_singleton"
        ),
        sa.CheckConstraint(
            "light_bypass_count >= 0",
            name="ck_evaluation_scheduler_state_bypass_count",
        ),
        sa.ForeignKeyConstraint(
            ["blocked_heavy_id"], ["evaluations.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO evaluation_scheduler_state "
        "(id, blocked_heavy_id, light_bypass_count) VALUES (1, NULL, 0)"
    )


def downgrade() -> None:
    op.drop_table("evaluation_scheduler_state")
