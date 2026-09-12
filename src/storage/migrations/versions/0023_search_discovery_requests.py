"""Add search_discovery_requests — the shared DB-as-event-broker table
between search repo and crawl (websearch.txt section 十/十五, final
"直写共享 DB" architecture, superseding the earlier HTTP-API draft).

search repo's WebSearch fallback (triggered when its own RAG has no
answer for a live user query) writes candidate URLs straight into this
table via its existing asyncpg pool — same Postgres instance, same
autism_crawler DB crawl already owns crawled_items in. crawl never
exposes an HTTP endpoint for this; search_queue_loop.py (a periodic
asyncio task, same shape as discovery_loop()) polls the table instead.

Column contract (websearch.txt section 十, "哪些列是契约"): search only
ever writes url/title/snippet/source_domain/trigger_query — those five
are the cross-repo contract and must not be renamed/retyped without
coordinating with search. status defaults to 'pending' on insert (search
never sets it explicitly). retry_count/last_http_status/next_retry_at/
error_note/processed_at are crawl-internal (search_queue_loop.py, see
15.2/15.3/15.4) — crawl can change these freely, search never reads or
writes them.

status values: pending / processing / done / failed / out_of_scope
(see websearch.txt section 十九 for out_of_scope's "留档供人工复核"
semantics — a domain outside crawl's tier1/2 allowlist is recorded, not
discarded).

Revision ID: 0023_search_discovery_requests
Revises: 0022_add_discovery_queue_state
Create Date: 2026-08-29
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_search_discovery_requests"
down_revision = "0022_add_discovery_queue_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_discovery_requests",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.Text(), nullable=True),
        sa.Column("trigger_query", sa.Text(), nullable=True),
        sa.Column(
            "discovered_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        # crawl-internal — see module docstring's "column contract" note.
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_http_status", sa.Integer(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_note", sa.Text(), nullable=True),
        sa.UniqueConstraint("url", name="search_discovery_requests_url_uq"),
    )
    op.create_index(
        "ix_search_discovery_requests_status",
        "search_discovery_requests",
        ["status", "discovered_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_search_discovery_requests_status", table_name="search_discovery_requests")
    op.drop_table("search_discovery_requests")
