"""Add IVFFLAT index on chunks.embedding for fast vector similarity search.

Without this index, chunk-level semantic search does a full sequential scan.
lists=30 is appropriate for ~300-500 chunks; revisit when chunks > 10,000.

Revision ID: 0005
Revises: 0004
Create Date: 2026-05-19
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Partial index — only rows that have an embedding.
    # No CONCURRENTLY needed: chunks table is small so the lock is instant.
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunk_embedding
        ON chunks
        USING ivfflat (embedding vector_cosine_ops)
        WITH (lists = 30)
        WHERE embedding IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_chunk_embedding")
