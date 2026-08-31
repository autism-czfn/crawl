"""Add is_stale flag to crawled_items for age-based staleness detection.

Items published more than 5 years ago are flagged stale at ingest time.

Revision ID: 0009_add_is_stale
Revises: 0008_add_needs_rechunk
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0009_add_is_stale"
down_revision = "0008_add_needs_rechunk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawled_items",
        sa.Column(
            "is_stale",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("crawled_items", "is_stale")
