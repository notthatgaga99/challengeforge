"""Add experimental workload_class to evaluations.

Revision ID: 0004_evaluation_workload_class
Revises: 0003_evaluation_pipeline
Create Date: 2026-09-22
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004_evaluation_workload_class"
down_revision: Union[str, Sequence[str], None] = "0003_evaluation_pipeline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "evaluations",
        sa.Column(
            "workload_class",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'light'"),
        ),
    )
    op.create_check_constraint(
        "ck_evaluations_workload_class",
        "evaluations",
        "workload_class IN ('light', 'medium', 'heavy')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_evaluations_workload_class", "evaluations", type_="check")
    op.drop_column("evaluations", "workload_class")
