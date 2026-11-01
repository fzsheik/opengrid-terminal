"""changed_at: when a recorded change happened (changed_at.py; methodology/freshness.md). Additive only.

    listing_observations.prev_observed_at     the last fetch that still showed the old value (the change bracket)
    listing_observations.provider_changed_at  the provider's own effective time of a price change, when it has one
    listing_observations.changed_at_basis     exact | date_only | lower_bound | poll_window

Existing rows keep NULLs until `python -m changed_at backfill` re-reads their raw snapshots (idempotent).

Revision ID: 0028_changed_at
Revises: 0026_fx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0028_changed_at"
down_revision: Union[str, Sequence[str], None] = "0026_fx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("listing_observations", sa.Column("prev_observed_at", TS, nullable=True))
    op.add_column("listing_observations", sa.Column("provider_changed_at", TS, nullable=True))
    op.add_column("listing_observations", sa.Column("changed_at_basis", sa.String(16), nullable=True))


def downgrade() -> None:
    for name in ("changed_at_basis", "provider_changed_at", "prev_observed_at"):
        op.drop_column("listing_observations", name)
