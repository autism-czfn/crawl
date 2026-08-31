"""Backfill authority_tier on existing crawled_items from surfaces table.

Fixes 1,374 rows where authority_tier IS NULL because they were collected
before pipeline.py propagated the surface's authority_tier to crawled_items.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-19
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE crawled_items ci
        SET authority_tier = s.authority_tier
        FROM surfaces s
        WHERE ci.surface_key = s.key
          AND s.authority_tier IS NOT NULL
          AND ci.authority_tier IS NULL
    """)


def downgrade() -> None:
    # Cannot safely reverse — we don't know which rows were originally NULL
    # vs. set by this migration. Leave as-is on downgrade.
    pass
