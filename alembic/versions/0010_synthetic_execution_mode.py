"""Allow synthetic_execution evaluation mode.

Revision ID: 0010_synthetic_execution_mode
Revises: 0009_interactive_hint_pool_wait
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_synthetic_execution_mode"
down_revision: Union[str, Sequence[str], None] = "0009_interactive_hint_pool_wait"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_evaluations_evaluation_mode", "evaluations", type_="check")
    op.create_check_constraint(
        "ck_evaluations_evaluation_mode",
        "evaluations",
        "evaluation_mode IN ("
        "'legacy', 'always_expensive', 'fixed_progressive', "
        "'resource_aware_adaptive', 'synthetic_execution'"
        ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_evaluations_evaluation_mode", "evaluations", type_="check")
    op.create_check_constraint(
        "ck_evaluations_evaluation_mode",
        "evaluations",
        "evaluation_mode IN ("
        "'legacy', 'always_expensive', 'fixed_progressive', 'resource_aware_adaptive'"
        ")",
    )
