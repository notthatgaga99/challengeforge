"""Create chunk_embeddings for dense retrieval (float array; pgvector optional).

Revision ID: 0014_chunk_embeddings
Revises: 0013_lexical_retrieval
Create Date: 2026-11-23

Stores embeddings as ``double precision[]`` so embedded Postgres / CI works
without the pgvector extension. Cosine similarity is computed in SQL via
dot-product on L2-normalized vectors (<=> equivalent when both are unit).
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_chunk_embeddings"
down_revision: Union[str, Sequence[str], None] = "0013_lexical_retrieval"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chunk_embeddings",
        sa.Column(
            "chunk_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("embedding_version", sa.Text(), primary_key=True),
        sa.Column(
            "embedding",
            postgresql.ARRAY(sa.Float()),
            nullable=False,
        ),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_chunk_embeddings_version",
        "chunk_embeddings",
        ["embedding_version"],
    )


def downgrade() -> None:
    op.drop_index("ix_chunk_embeddings_version", table_name="chunk_embeddings")
    op.drop_table("chunk_embeddings")
