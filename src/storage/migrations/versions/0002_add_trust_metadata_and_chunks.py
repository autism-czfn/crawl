"""Add trust metadata to surfaces and crawled_items, add chunks table.

Revision ID: 0002
Revises: 0001_initial
Create Date: 2026-04-16
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Surface model: add trust metadata columns ---
    op.add_column("surfaces", sa.Column("authority_tier", sa.Integer(), nullable=True))
    op.add_column("surfaces", sa.Column("source_type", sa.Text(), nullable=True))
    op.add_column("surfaces", sa.Column("audience_type", sa.Text(), nullable=True))
    op.add_column("surfaces", sa.Column("language", sa.Text(), nullable=True, server_default="en"))
    op.add_column("surfaces", sa.Column("country", sa.Text(), nullable=True))
    op.add_column("surfaces", sa.Column("organization_name", sa.Text(), nullable=True))
    op.add_column("surfaces", sa.Column("force_recrawl", sa.Boolean(), nullable=False, server_default="false"))

    # --- CrawledItem model: add trust metadata columns ---
    op.add_column("crawled_items", sa.Column("authority_tier", sa.Integer(), nullable=True))
    op.add_column("crawled_items", sa.Column("source_type", sa.Text(), nullable=True))
    op.add_column("crawled_items", sa.Column("audience_type", sa.Text(), nullable=True))

    # --- Chunks table ---
    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("crawled_item_id", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("chunk_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("embedding_model", sa.Text(), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_chunk_item_index", "chunks", ["crawled_item_id", "chunk_index"])
    op.create_index("idx_chunk_item", "chunks", ["crawled_item_id", "chunk_index"])
    op.create_index("idx_chunk_crawled_item_id", "chunks", ["crawled_item_id"])

    # --- Content freshness columns (P5) ---
    op.add_column("crawled_items", sa.Column("content_hash", sa.Text(), nullable=True))
    op.add_column("crawled_items", sa.Column("content_updated_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("crawled_items", "content_updated_at")
    op.drop_column("crawled_items", "content_hash")

    op.drop_index("idx_chunk_crawled_item_id", table_name="chunks")
    op.drop_index("idx_chunk_item", table_name="chunks")
    op.drop_constraint("uq_chunk_item_index", "chunks", type_="unique")
    op.drop_table("chunks")

    op.drop_column("crawled_items", "audience_type")
    op.drop_column("crawled_items", "source_type")
    op.drop_column("crawled_items", "authority_tier")

    op.drop_column("surfaces", "organization_name")
    op.drop_column("surfaces", "country")
    op.drop_column("surfaces", "language")
    op.drop_column("surfaces", "audience_type")
    op.drop_column("surfaces", "source_type")
    op.drop_column("surfaces", "force_recrawl")
    op.drop_column("surfaces", "authority_tier")
