"""Add discovery_queue_state — durable round-robin cursor for the LLM
websearch discovery layer (crawl.txt section 13, "第4层" claude -p
WebSearch supplement).

discovery_loop() will work through a round-robin queue of ~25-50
(domain, topic) pairs derived from config/surfaces.json's tier1/2
html_crawl/playwright_crawl/sitemap surfaces (see src/discovery/queue.py),
processing one pair per hour. If that position only lived in an in-memory
variable, every crawler restart (this repo's crawler.log shows several a
day — manual stop/restart cycles, no supervisor) would reset it back to
pair #1, so some pairs would get checked far more often than others and
some might never be reached. This single-row table persists the cursor
across restarts, the same durability problem blocked_domains
(migration 0021) solved for a different loop.

Single-row table (id fixed at 1, enforced by a CHECK constraint rather
than a separate lookup) — there is exactly one discovery_loop, so no
need for a key per anything.

Revision ID: 0022_add_discovery_queue_state
Revises: 0021_add_blocked_domains
Create Date: 2026-08-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_add_discovery_queue_state"
down_revision = "0021_add_blocked_domains"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "discovery_queue_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        # -1 so the first-ever run computes (last_index + 1) % len(queue) == 0,
        # i.e. starts at the front of the queue rather than skipping pair #0.
        sa.Column("last_index", sa.Integer(), nullable=False, server_default="-1"),
        sa.Column("last_pair_domain", sa.Text(), nullable=True),
        sa.Column("last_pair_topic", sa.Text(), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_discovery_queue_state_singleton"),
    )
    op.execute("INSERT INTO discovery_queue_state (id, last_index) VALUES (1, -1)")


def downgrade() -> None:
    op.drop_table("discovery_queue_state")
