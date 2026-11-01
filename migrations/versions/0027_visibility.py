"""visibility: the data tier of every listing, capacity row and archived price (methodology/data-visibility.md).
Additive only: one NOT NULL column with a server default, so every existing row is 'public' (the default tier).

    compute_listings.visibility   'public' | 'internal' (CHECK); listing_observations inherit it via their listing
    capacity_current.visibility   'public' | 'internal'
    capacity_changes.visibility   'public' | 'internal'
    archived_prices.visibility    'public' | 'internal'

Which sources are internal is decided in code (provider_meta.INTERNAL_SOURCES) and stamped on each row when it
is saved; public readers filter visibility = 'public'.

Revision ID: 0027_visibility
Revises: 0026_fx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0027_visibility"
down_revision: Union[str, Sequence[str], None] = "0026_fx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLES = ("compute_listings", "capacity_current", "capacity_changes", "archived_prices")


def upgrade() -> None:
    for t in TABLES:
        # A constant server default: Postgres adds the column without rewriting the table.
        op.add_column(t, sa.Column("visibility", sa.String(8), nullable=False, server_default="public"))
    op.create_check_constraint("ck_listing_visibility", "compute_listings", "visibility IN ('public', 'internal')")


def downgrade() -> None:
    op.drop_constraint("ck_listing_visibility", "compute_listings", type_="check")
    for t in reversed(TABLES):
        op.drop_column(t, "visibility")
