"""Add blocked_domains table — durable "give up" tracking per domain.

src/http/client.py already has an in-memory CircuitBreaker (threshold=5,
300s cooldown) that pauses requests to a misbehaving domain temporarily —
but it resets on every process restart, and re-opens/closes forever for a
domain that will NEVER stop 403-blocking us (confirmed for at least one
comparable domain — see chop_adhd's content_notes in config/surfaces.json:
even a real Playwright browser still got 403'd, an edge/network-level
block that doesn't resolve on its own).

This table is a separate, durable, permanent decision: once a domain
crosses consecutive_403_count >= 5 across enrich_fulltext() cycles (see
src/pipeline.py), it is marked given_up=True and every future oa_url on
that domain is immediately written as the '' permanent-failure sentinel
instead of being fetched at all — no more retries, no more wasted requests,
and the item stops occupying a slot in the "awaiting fulltext" queue.

Revision ID: 0021_add_blocked_domains
Revises: 0020_quality_metrics_views
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_add_blocked_domains"
down_revision = "0020_quality_metrics_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blocked_domains",
        sa.Column("domain", sa.Text(), primary_key=True),
        sa.Column("consecutive_403_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("given_up", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("given_up_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_checked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("blocked_domains")
