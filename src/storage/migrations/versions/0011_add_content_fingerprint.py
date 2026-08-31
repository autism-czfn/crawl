"""Add content_fingerprint JSONB column to crawled_items for near-duplicate detection.

Stores top-50 3-word shingles as a fingerprint array.
Full similarity query is a future optimization (see TODO in pipeline.py).

Revision ID: 0011_add_content_fingerprint
Revises: 0010_add_evidence_level
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0011_add_content_fingerprint"
down_revision = "0010_add_evidence_level"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawled_items",
        sa.Column("content_fingerprint", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crawled_items", "content_fingerprint")
