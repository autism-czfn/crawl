"""Add domain_tags/topic_tags JSONB columns for the sleep/eating/adhd expansion.

Multi-domain classification: a crawled item can belong to more than one
domain (e.g. an autism+ADHD comorbidity paper matched by both pubmed_autism
and pubmed_adhd) — domain_tags is a JSONB list, unioned (not overwritten) on
upsert by pipeline.py::_merge_tag_list. topic_tags is a JSONB list from the
controlled vocabulary documented in src/collectors/*.py surface configs
(sleep_latency, night_waking, arfid, executive_function, etc).

Columns added to:
  - crawled_items (the actual per-item values)
  - surfaces      (static per-surface default, synced from surfaces.json)
  - chunks        (propagated from the parent crawled_item at chunk-insert time)

Backfill: every row that predates this migration was collected before any
non-autism surface existed, so it is unambiguously domain_tags=["autism"].

Revision ID: 0019_add_domain_topic_tags
Revises: 0018_hnsw_index
Create Date: 2026-08-24
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0019_add_domain_topic_tags"
down_revision = "0018_hnsw_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crawled_items", sa.Column("domain_tags", postgresql.JSONB(), nullable=True))
    op.add_column("crawled_items", sa.Column("topic_tags", postgresql.JSONB(), nullable=True))
    op.add_column("surfaces", sa.Column("domain_tags", postgresql.JSONB(), nullable=True))
    op.add_column("surfaces", sa.Column("topic_tags", postgresql.JSONB(), nullable=True))
    op.add_column("chunks", sa.Column("domain_tags", postgresql.JSONB(), nullable=True))
    op.add_column("chunks", sa.Column("topic_tags", postgresql.JSONB(), nullable=True))

    # Backfill: all pre-existing crawled_items predate this feature and are
    # autism-only by construction (this crawler had no other topic before
    # the sleep/eating/adhd expansion).
    op.execute(
        """
        UPDATE crawled_items
        SET domain_tags = '["autism"]'::jsonb
        WHERE domain_tags IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("chunks", "topic_tags")
    op.drop_column("chunks", "domain_tags")
    op.drop_column("surfaces", "topic_tags")
    op.drop_column("surfaces", "domain_tags")
    op.drop_column("crawled_items", "topic_tags")
    op.drop_column("crawled_items", "domain_tags")
