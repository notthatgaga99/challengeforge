"""Create document_chunks for durable retrieval-oriented chunk sets.

Revision ID: 0012_document_chunks
Revises: 0011_ingestion_jobs
Create Date: 2026-09-23
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_document_chunks"
down_revision: Union[str, Sequence[str], None] = "0011_ingestion_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ingestion_job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ingestion_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_key", sa.Text(), nullable=False),
        sa.Column("parser_version", sa.Text(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("line_start", sa.Integer(), nullable=False),
        sa.Column("line_end", sa.Integer(), nullable=False),
        sa.Column("block_type", sa.Text(), nullable=False),
        sa.Column(
            "heading_path",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "oversized_split",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name="ck_document_chunks_ordinal"),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end >= char_start",
            name="ck_document_chunks_chars",
        ),
        sa.CheckConstraint(
            "line_start >= 1 AND line_end >= line_start",
            name="ck_document_chunks_lines",
        ),
        sa.UniqueConstraint(
            "ingestion_job_id",
            "ordinal",
            name="uq_document_chunks_job_ordinal",
        ),
    )
    op.create_index(
        "ix_document_chunks_artifact_key",
        "document_chunks",
        ["artifact_key"],
    )
    op.create_index(
        "ix_document_chunks_job",
        "document_chunks",
        ["ingestion_job_id"],
    )
    op.create_index(
        "ix_document_chunks_versions",
        "document_chunks",
        ["parser_version", "chunker_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_document_chunks_versions", table_name="document_chunks")
    op.drop_index("ix_document_chunks_job", table_name="document_chunks")
    op.drop_index("ix_document_chunks_artifact_key", table_name="document_chunks")
    op.drop_table("document_chunks")
