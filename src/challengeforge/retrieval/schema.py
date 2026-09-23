"""Ensure lexical FTS columns/indexes exist (for create_all test DBs)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


_ENSURE_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'document_chunks' AND column_name = 'search_tsv'
  ) THEN
    ALTER TABLE document_chunks
    ADD COLUMN search_tsv tsvector
    GENERATED ALWAYS AS (
      setweight(to_tsvector('english', coalesce(heading_path::text, '')), 'A')
      || setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes WHERE indexname = 'ix_document_chunks_search_tsv'
  ) THEN
    CREATE INDEX ix_document_chunks_search_tsv
    ON document_chunks USING GIN (search_tsv);
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM information_schema.columns
    WHERE table_name = 'document_chunks' AND column_name = 'search_tsv_simple'
  ) THEN
    ALTER TABLE document_chunks
    ADD COLUMN search_tsv_simple tsvector
    GENERATED ALWAYS AS (
      to_tsvector('simple', coalesce(content, ''))
    ) STORED;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE indexname = 'ix_document_chunks_search_tsv_simple'
  ) THEN
    CREATE INDEX ix_document_chunks_search_tsv_simple
    ON document_chunks USING GIN (search_tsv_simple);
  END IF;
END $$;
"""


async def ensure_lexical_search_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(_ENSURE_SQL))


async def ensure_lexical_search_schema_conn(conn: AsyncConnection) -> None:
    await conn.execute(text(_ENSURE_SQL))
