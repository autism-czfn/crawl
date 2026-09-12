"""Add crawled_items.fetch_attempts and blocked_domains.ever_succeeded /
first_failure_at — replaces the 403-only, count-based give-up rule with:

  URL-level:    give up on one URL after 3 failed attempts (any failure
                type), regardless of its domain's status.
  Domain-level: give up on a WHOLE domain only after 24h of zero successes
                since its first-ever failure — and ONLY if that domain has
                NEVER succeeded even once (ever_succeeded=False). A domain
                that has succeeded before is never blacklisted wholesale;
                its individual bad URLs still self-eliminate via the
                URL-level 3-attempts rule above.

See src/pipeline.py enrich_fulltext() for the logic. Supersedes the old
consecutive_403_count-driven _GIVE_UP_403_THRESHOLD check from migration
0021 (that column is left in place, still informational, just no longer
what decides given_up).

Revision ID: 0025_give_up_rules
Revises: 0024_classifier_fields
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_give_up_rules"
down_revision = "0024_classifier_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "crawled_items",
        sa.Column("fetch_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "blocked_domains",
        sa.Column("ever_succeeded", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "blocked_domains",
        sa.Column("first_failure_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("blocked_domains", "first_failure_at")
    op.drop_column("blocked_domains", "ever_succeeded")
    op.drop_column("crawled_items", "fetch_attempts")
