"""Add near_duplicate_of column to crawled_items for near-duplicate detection.

Stores the URL of the canonical item when a near-duplicate is detected (>70%
shingle overlap). The duplicate is still stored — it may have fresher metadata.

Revision ID: 0016_add_near_duplicate_of
Revises: 0015_reset_embeddings_for_prefix
Create Date: 2026-05-19
"""
from alembic import op

revision = "0016_add_near_duplicate_of"
down_revision = "0015_reset_embeddings_for_prefix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE crawled_items ADD COLUMN IF NOT EXISTS near_duplicate_of TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE crawled_items DROP COLUMN IF EXISTS near_duplicate_of")
