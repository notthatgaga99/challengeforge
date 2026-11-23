"""Create rate_limit_buckets for abuse budgets.

Revision ID: 0017_rate_limit_buckets
Revises: 0016_outbox_events
Create Date: 2026-11-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_rate_limit_buckets"
down_revision: Union[str, Sequence[str], None] = "0016_outbox_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action", sa.Text(), primary_key=True),
        sa.Column(
            "window_started_at", sa.DateTime(timezone=True), primary_key=True
        ),
        sa.Column(
            "count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("count >= 0", name="ck_rate_limit_buckets_count"),
    )
    op.create_index(
        "ix_rate_limit_buckets_window",
        "rate_limit_buckets",
        ["window_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rate_limit_buckets_window", table_name="rate_limit_buckets")
    op.drop_table("rate_limit_buckets")
