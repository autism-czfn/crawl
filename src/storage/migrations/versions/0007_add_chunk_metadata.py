"""Add metadata columns to chunks table for richer retrieval context.

Revision ID: 0007_add_chunk_metadata
Revises: 0006
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_add_chunk_metadata"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("title", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("url", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("domain", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("source_type", sa.Text(), nullable=True))
    op.add_column("chunks", sa.Column("authority_tier", sa.Integer(), nullable=True))
    op.add_column("chunks", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("chunks", sa.Column("lang", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunks", "title")
    op.drop_column("chunks", "url")
    op.drop_column("chunks", "domain")
    op.drop_column("chunks", "source_type")
    op.drop_column("chunks", "authority_tier")
    op.drop_column("chunks", "published_at")
    op.drop_column("chunks", "lang")
