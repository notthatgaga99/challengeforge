"""Add lexical full-text search support on document_chunks.

Revision ID: 0013_lexical_retrieval
Revises: 0012_document_chunks
Create Date: 2026-09-23

Adds a stored ``search_tsv`` column (english weighted content + heading path)
and a GIN index. Ranking uses PostgreSQL ``ts_rank_cd`` — **not** BM25.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013_lexical_retrieval"
down_revision: Union[str, Sequence[str], None] = "0012_document_chunks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            ALTER TABLE document_chunks
            ADD COLUMN search_tsv tsvector
            GENERATED ALWAYS AS (
                setweight(
                    to_tsvector(
                        'english',
                        coalesce(heading_path::text, '')
                    ),
                    'A'
                )
                ||
                setweight(
                    to_tsvector('english', coalesce(content, '')),
                    'B'
                )
            ) STORED
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_document_chunks_search_tsv
            ON document_chunks
            USING GIN (search_tsv)
            """
        )
    )
    # Secondary expression for identifier-ish matching (simple config, no stemming).
    op.execute(
        sa.text(
            """
            ALTER TABLE document_chunks
            ADD COLUMN search_tsv_simple tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(content, ''))
            ) STORED
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE INDEX ix_document_chunks_search_tsv_simple
            ON document_chunks
            USING GIN (search_tsv_simple)
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_document_chunks_search_tsv_simple"))
    op.execute(
        sa.text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_tsv_simple")
    )
    op.execute(sa.text("DROP INDEX IF EXISTS ix_document_chunks_search_tsv"))
    op.execute(sa.text("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_tsv"))
