"""Persist resource-aware runtime pressure on scheduler singleton.

Revision ID: 0006_resource_aware_runtime
Revises: 0005_bounded_light_bypass
Create Date: 2026-09-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_resource_aware_runtime"
down_revision: Union[str, Sequence[str], None] = "0005_bounded_light_bypass"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluation_scheduler_state",
        sa.Column(
            "pressure_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
    )
    op.add_column(
        "evaluation_scheduler_state",
        sa.Column("adaptive_max_workers", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("evaluation_scheduler_state", "adaptive_max_workers")
    op.drop_column("evaluation_scheduler_state", "pressure_state")
