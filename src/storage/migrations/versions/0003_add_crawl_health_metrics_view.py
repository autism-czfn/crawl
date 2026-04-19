"""Add crawl_health_metrics view for per-tier freshness reporting.

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-18

Provides search /api/stats with per-authority-tier freshness data without
adding an HTTP server to the crawl service. Search reads this view via
DATABASE_URL (the shared autism_crawler connection).
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW crawl_health_metrics AS
        SELECT
            authority_tier,
            COUNT(*)                                                    AS source_count,
            MAX(collected_at)                                           AS last_item_at,
            COUNT(*) FILTER (
                WHERE collected_at >= NOW() - INTERVAL '7 days'
            )                                                           AS items_last_7d,
            (MAX(collected_at) < NOW() - INTERVAL '7 days')            AS staleness_flag
        FROM crawled_items
        GROUP BY authority_tier
        ORDER BY authority_tier NULLS LAST
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS crawl_health_metrics")
