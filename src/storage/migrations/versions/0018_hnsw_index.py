"""Replace IVFFlat vector index with HNSW for better ANN recall at scale.

HNSW (Hierarchical Navigable Small World) provides:
  - Better recall at the same ef_search value vs IVFFlat probes
  - No need for a training step (IVFFlat requires k-means on existing data)
  - Consistent performance as the dataset grows

Parameters:
  m=16              — number of connections per layer (higher → better recall, more memory)
  ef_construction=64 — build-time quality parameter (higher → slower build, better recall)
  ef_search=40      — query-time quality parameter (set per-connection in search code)

Revision ID: 0018_hnsw_index
Revises: 0017_embed_schema_version
Create Date: 2026-05-19
"""
from alembic import op

revision = "0018_hnsw_index"
down_revision = "0017_embed_schema_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop existing IVFFlat indexes (if they exist)
    op.execute("DROP INDEX IF EXISTS crawled_items_embedding_idx")
    op.execute("DROP INDEX IF EXISTS chunks_embedding_idx")

    # Create HNSW indexes
    # m=16 (connections per layer), ef_construction=64 (build quality)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_crawled_items_embedding_hnsw
        ON crawled_items
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw
        ON chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_crawled_items_embedding_hnsw")
    op.execute("DROP INDEX IF EXISTS idx_chunks_embedding_hnsw")
