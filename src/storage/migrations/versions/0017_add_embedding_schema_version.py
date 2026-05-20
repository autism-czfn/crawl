"""Add embedding_schema_version column to crawled_items and chunks.

Tracks which embedding schema version (prefix convention, chunking strategy)
was used to generate the vector. Increment EMBEDDING_SCHEMA_VERSION in
crawl/src/embeddings.py to trigger re-embedding.

Revision ID: 0017_embed_schema_version
Revises: 0016_add_near_duplicate_of
Create Date: 2026-05-19
"""
from alembic import op

revision = "0017_embed_schema_version"
down_revision = "0016_add_near_duplicate_of"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE crawled_items ADD COLUMN IF NOT EXISTS embedding_schema_version TEXT")
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_schema_version TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE crawled_items DROP COLUMN IF EXISTS embedding_schema_version")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS embedding_schema_version")
