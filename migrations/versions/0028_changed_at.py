"""changed_at: when a recorded change happened (changed_at.py; methodology/freshness.md). Additive only.

    listing_observations.prev_observed_at     the last fetch that still showed the old value (the change bracket)
    listing_observations.provider_changed_at  the provider's own effective time of a price change, when it has one
    listing_observations.changed_at_basis     exact | date_only | lower_bound | poll_window
    raw_snapshots.throttled                   a 429 (or a call held back for the provider's rate limit): not a failure
                                              (+ partial index ix_raw_throttled_time, small: throttled rows only)
    freshness_log                             per poll and stream (price | capacity): how new the stored data was

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
    op.add_column("raw_snapshots", sa.Column("throttled", sa.Boolean(), nullable=True))
    op.create_index("ix_raw_throttled_time", "raw_snapshots", ["fetched_at"], postgresql_where=sa.text("throttled"))
    op.create_table(
        "freshness_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("logged_at", TS, nullable=False),
        sa.Column("data_at", TS, nullable=False),
    )
    op.create_index("ix_freshness_log_key_time", "freshness_log", ["provider", "stream", "logged_at"])
    op.create_index("ix_freshness_log_time", "freshness_log", ["logged_at"])


def downgrade() -> None:
    op.drop_table("freshness_log")
    op.drop_index("ix_raw_throttled_time", table_name="raw_snapshots")
    op.drop_column("raw_snapshots", "throttled")
    for name in ("changed_at_basis", "provider_changed_at", "prev_observed_at"):
        op.drop_column("listing_observations", name)
