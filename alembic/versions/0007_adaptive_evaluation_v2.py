"""Progressive evaluation durable stage fields.

Revision ID: 0007_adaptive_evaluation_v2
Revises: 0006_resource_aware_runtime
Create Date: 2026-09-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007_adaptive_evaluation_v2"
down_revision: Union[str, Sequence[str], None] = "0006_resource_aware_runtime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluations",
        sa.Column(
            "current_stage",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "evaluations",
        sa.Column(
            "evaluation_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'legacy'"),
        ),
    )
    op.add_column(
        "evaluations",
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_evaluations_current_stage",
        "evaluations",
        "current_stage >= 0",
    )
    op.create_check_constraint(
        "ck_evaluations_evaluation_mode",
        "evaluations",
        "evaluation_mode IN ("
        "'legacy', 'always_expensive', 'fixed_progressive', 'resource_aware_adaptive'"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_evaluations_evaluation_mode", "evaluations", type_="check")
    op.drop_constraint("ck_evaluations_current_stage", "evaluations", type_="check")
    op.drop_column("evaluations", "deadline_at")
    op.drop_column("evaluations", "evaluation_mode")
    op.drop_column("evaluations", "current_stage")
