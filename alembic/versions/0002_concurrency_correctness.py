"""Add idempotency request fingerprint.

Revision ID: 0002_concurrency_correctness
Revises: 0001_initial
Create Date: 2026-09-21
"""

from __future__ import annotations

import hashlib
import json
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_concurrency_correctness"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fingerprint(challenge_id: object, metadata: object) -> str:
    canonical = json.dumps(
        {
            "challenge_id": str(challenge_id),
            "metadata": metadata or {},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def upgrade() -> None:
    op.add_column(
        "submissions",
        sa.Column("request_fingerprint", sa.Text(), nullable=True),
    )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, challenge_id, metadata
            FROM submissions
            WHERE idempotency_key IS NOT NULL
            """
        )
    )
    for row in rows:
        mapping = row._mapping
        bind.execute(
            sa.text(
                """
                UPDATE submissions
                SET request_fingerprint = :fingerprint
                WHERE id = :submission_id
                """
            ),
            {
                "submission_id": mapping["id"],
                "fingerprint": _fingerprint(
                    mapping["challenge_id"], mapping["metadata"]
                ),
            },
        )

    op.create_check_constraint(
        "ck_submissions_idempotency_fingerprint",
        "submissions",
        """
        (idempotency_key IS NULL AND request_fingerprint IS NULL)
        OR
        (idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)
        """,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_submissions_idempotency_fingerprint",
        "submissions",
        type_="check",
    )
    op.drop_column("submissions", "request_fingerprint")

