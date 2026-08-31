"""Add GIN full-text search index on chunks.chunk_text for BM25-style retrieval.

Adds a generated tsvector column and a GIN index for fast keyword search
alongside vector similarity search.

Revision ID: 0013_add_chunk_fts_index
Revises: 0012_add_chunk_is_summary
Create Date: 2026-05-19
"""
from alembic import op

revision = "0013_add_chunk_fts_index"
down_revision = "0012_add_chunk_is_summary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE chunks
        ADD COLUMN IF NOT EXISTS chunk_tsv tsvector
            GENERATED ALWAYS AS (to_tsvector('english', coalesce(chunk_text, ''))) STORED
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunk_fts
        ON chunks
        USING GIN(chunk_tsv)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunk_fts")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS chunk_tsv")
