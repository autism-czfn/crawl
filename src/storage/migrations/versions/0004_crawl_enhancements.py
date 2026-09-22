"""Add oa_url, last_harvested_at to crawled_items; overrides_json to surfaces.

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crawled_items", sa.Column("oa_url", sa.Text(), nullable=True))
    op.add_column("crawled_items", sa.Column("last_harvested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("surfaces", sa.Column("overrides_json", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("crawled_items", "oa_url")
    op.drop_column("crawled_items", "last_harvested_at")
    op.drop_column("surfaces", "overrides_json")
