"""Add quality-metrics views for the sleep/eating/adhd expansion (spec section 12).

Deliberately additive — does NOT modify the existing crawl_health_metrics
view (0003), since its docstring notes an external search service reads it
via DATABASE_URL and its column shape should stay stable.

Three new views, each grouped at the granularity its metric actually needs
(a single view can't cleanly mix per-surface, per-evidence-level, and
per-domain-tag rows):

  crawl_health_metrics_by_surface
    Per-surface freshness + extraction quality + duplicate rate. Answers
    "full-body extraction success rate per surface" and "duplicate rate".

  crawl_evidence_level_distribution
    COUNT(*) per evidence_level value (including the new preprint/
    clinical_trial/government_guidance/hospital_education/peer_reviewed_study
    values added alongside this expansion, and NULL for items with no
    inferred level).

  crawl_domain_tag_coverage
    COUNT(*) per domain_tags value, via jsonb_array_elements_text — an item
    tagged with more than one domain (e.g. ["adhd","autism"]) is correctly
    counted once under each domain it belongs to, not just its first tag.

Revision ID: 0020_quality_metrics_views
Revises: 0019_add_domain_topic_tags
Create Date: 2026-08-25

NOTE: revision id kept short deliberately — alembic_version.version_num is
VARCHAR(32) in this DB, and a longer id (originally
"0020_add_expansion_quality_metrics", 34 chars) overflowed it. Caught by
actually running this migration against the live dev DB rather than only
compiling it; the whole upgrade rolled back cleanly with no partial state
when it happened, so no manual cleanup was needed, but it's worth new
migrations staying under ~32 chars for the revision id specifically (the
docstring/filename can be as descriptive as needed).
"""
from alembic import op

revision = "0020_quality_metrics_views"
down_revision = "0019_add_domain_topic_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE OR REPLACE VIEW crawl_health_metrics_by_surface AS
        SELECT
            surface_key,
            MAX(authority_tier)                                          AS authority_tier,
            MAX(source_type)                                             AS source_type,
            COUNT(*)                                                     AS source_count,
            COUNT(*) FILTER (WHERE content_body IS NOT NULL)             AS with_content_body,
            ROUND(
                COUNT(*) FILTER (WHERE content_body IS NOT NULL)::numeric
                / NULLIF(COUNT(*), 0),
                4
            )                                                             AS extraction_success_rate,
            COUNT(*) FILTER (WHERE near_duplicate_of IS NOT NULL)        AS duplicate_count,
            ROUND(
                COUNT(*) FILTER (WHERE near_duplicate_of IS NOT NULL)::numeric
                / NULLIF(COUNT(*), 0),
                4
            )                                                             AS duplicate_rate,
            MAX(collected_at)                                            AS last_item_at,
            COUNT(*) FILTER (
                WHERE collected_at >= NOW() - INTERVAL '7 days'
            )                                                             AS items_last_7d,
            (MAX(collected_at) < NOW() - INTERVAL '7 days')              AS staleness_flag
        FROM crawled_items
        GROUP BY surface_key
        ORDER BY surface_key
    """)

    op.execute("""
        CREATE OR REPLACE VIEW crawl_evidence_level_distribution AS
        SELECT
            evidence_level,
            COUNT(*) AS item_count
        FROM crawled_items
        GROUP BY evidence_level
        ORDER BY item_count DESC NULLS LAST
    """)

    op.execute("""
        CREATE OR REPLACE VIEW crawl_domain_tag_coverage AS
        SELECT
            domain_value AS domain_tag,
            COUNT(*)     AS item_count
        FROM crawled_items,
             LATERAL jsonb_array_elements_text(
                 COALESCE(domain_tags, '[]'::jsonb)
             ) AS domain_value
        GROUP BY domain_value
        ORDER BY item_count DESC
    """)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS crawl_domain_tag_coverage")
    op.execute("DROP VIEW IF EXISTS crawl_evidence_level_distribution")
    op.execute("DROP VIEW IF EXISTS crawl_health_metrics_by_surface")
