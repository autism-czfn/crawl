"""Add GIN full-text search index on crawled_items for keyword search.

search/src/search/keyword.py's inline to_tsvector query had no matching
index, forcing a full sequential scan on every keyword search (confirmed via
EXPLAIN ANALYZE: ~179s on a 3-term query, 93k rows, parallel seq scan).

Adds a generated tsvector column over title/description/content_body and a
GIN index on it, built CONCURRENTLY to avoid blocking writes.

Revision ID: 0026_add_crawled_items_fts_index
Revises: 0025_give_up_rules
Create Date: 2026-09-22
"""
from alembic import op

revision = "0026_add_crawled_items_fts_index"
down_revision = "0025_give_up_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE crawled_items
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('english',
                    coalesce(title, '') || ' ' ||
                    coalesce(description, '') || ' ' ||
                    coalesce(content_body, '')
                )
            ) STORED
    """)
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_crawled_items_fts
            ON crawled_items
            USING GIN(content_tsv)
        """)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_crawled_items_fts")
    op.execute("ALTER TABLE crawled_items DROP COLUMN IF EXISTS content_tsv")
