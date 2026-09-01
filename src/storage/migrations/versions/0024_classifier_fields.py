"""Add classifier_tier/classifier_confidence/classifier_reason/
promoted_surface_key to search_discovery_requests — the auto-promotion
feature (src/discovery/classifier.py + src/discovery/surfaces_writer.py)
that makes websearch.txt section 十九's "留档供人工复核" automatic
instead of a dead-end status nothing ever consumed.

All four are crawl-internal (see 0023's "column contract" note, carried
forward in the model's docstring) — search never writes or reads these.

Revision ID: 0024_classifier_fields
Revises: 0023_search_discovery_requests
Create Date: 2026-08-31
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_classifier_fields"
down_revision = "0023_search_discovery_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("search_discovery_requests", sa.Column("classifier_tier", sa.Integer(), nullable=True))
    op.add_column("search_discovery_requests", sa.Column("classifier_confidence", sa.Text(), nullable=True))
    op.add_column("search_discovery_requests", sa.Column("classifier_reason", sa.Text(), nullable=True))
    op.add_column("search_discovery_requests", sa.Column("promoted_surface_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("search_discovery_requests", "promoted_surface_key")
    op.drop_column("search_discovery_requests", "classifier_reason")
    op.drop_column("search_discovery_requests", "classifier_confidence")
    op.drop_column("search_discovery_requests", "classifier_tier")
