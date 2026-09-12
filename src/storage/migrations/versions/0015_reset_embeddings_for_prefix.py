"""Reset all embeddings so they are re-generated with nomic task prefixes.

nomic-embed-text-v1.5 requires "search_document: " prefix for documents
and "search_query: " prefix for queries. Existing embeddings were generated
without these prefixes and must be reset to NULL so they are re-embedded.

Revision ID: 0015_reset_embeddings_for_prefix
Revises: 0014_add_chunk_heading
Create Date: 2026-05-19
"""
from alembic import op

revision = "0015_reset_embeddings_for_prefix"
down_revision = "0014_add_chunk_heading"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE crawled_items SET embedding = NULL, embedded_at = NULL")
    op.execute("UPDATE chunks SET embedding = NULL, embedded_at = NULL")


def downgrade() -> None:
    pass  # can't restore embeddings — they must be re-generated
