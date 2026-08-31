"""Add section_heading and heading_path columns to chunks table.

Revision ID: 0014_add_chunk_heading
Revises: 0013_add_chunk_fts_index
Create Date: 2026-05-19
"""
from alembic import op

revision = "0014_add_chunk_heading"
down_revision = "0013_add_chunk_fts_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS section_heading TEXT")
    op.execute("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS heading_path TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS section_heading")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS heading_path")
