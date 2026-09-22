"""Add evidence_level column to crawled_items for medical domain ranking.

Values: systematic_review, rct, cohort, case_study, expert_opinion,
        guideline, peer_reviewed, blog, anecdotal, or NULL.

Revision ID: 0010_add_evidence_level
Revises: 0009_add_is_stale
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_add_evidence_level"
down_revision = "0009_add_is_stale"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawled_items",
        sa.Column("evidence_level", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("crawled_items", "evidence_level")
