"""Add needs_rechunk flag to crawled_items for stale-chunk invalidation.

When content changes on re-crawl, needs_rechunk is set to True.
The chunk pipeline deletes old chunks and re-chunks those items.

Revision ID: 0008_add_needs_rechunk
Revises: 0007_add_chunk_metadata
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_add_needs_rechunk"
down_revision = "0007_add_chunk_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawled_items",
        sa.Column(
            "needs_rechunk",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("crawled_items", "needs_rechunk")
