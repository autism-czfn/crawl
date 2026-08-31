"""Add is_summary flag to chunks for parent-child retrieval infrastructure.

Summary chunks (chunk_index=-1, is_summary=True) serve as document-level
embedding anchors for coarse-grained retrieval before fine-grained chunk
retrieval.

Revision ID: 0012_add_chunk_is_summary
Revises: 0011_add_content_fingerprint
Create Date: 2026-05-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0012_add_chunk_is_summary"
down_revision = "0011_add_content_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column(
            "is_summary",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("chunks", "is_summary")
