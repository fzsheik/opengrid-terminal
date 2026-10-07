"""market events (store/events.py)

Revision ID: 0004_events
Revises: 0003_structure
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0004_events"
down_revision: Union[str, Sequence[str], None] = "0003_structure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("segment", sa.String(16), nullable=True),
        sa.Column("gpu", sa.String(160), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("region_group", sa.String(32), nullable=True),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
        sa.Column("value_before", sa.Numeric(14, 6), nullable=True),
        sa.Column("value_after", sa.Numeric(14, 6), nullable=True),
        sa.Column("pct", sa.Float(), nullable=True),
        sa.Column("dedupe_key", sa.String(300), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key"),
    )
    op.create_index("ix_me_occurred", "market_events", ["occurred_at"])
    op.create_index("ix_me_gpu_occurred", "market_events", ["gpu", "occurred_at"])
    op.create_index("ix_me_provider_occurred", "market_events", ["provider", "occurred_at"])
    op.create_index("ix_me_type_occurred", "market_events", ["type", "occurred_at"])

    op.create_table(
        "market_gpu_hourly",
        sa.Column("segment", sa.String(16), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("hour", sa.DateTime(timezone=True), nullable=False),
        sa.Column("providers_live", sa.Integer(), nullable=False),
        sa.Column("providers_priced", sa.Integer(), nullable=False),
        sa.Column("providers_available", sa.Integer(), nullable=False),
        sa.Column("live_listings", sa.Integer(), nullable=False),
        sa.Column("priced_listings", sa.Integer(), nullable=False),
        sa.Column("available_listings", sa.Integer(), nullable=False),
        sa.Column("sold_out_listings", sa.Integer(), nullable=False),
        sa.Column("lowest", sa.Numeric(14, 6), nullable=True),
        sa.Column("median", sa.Numeric(14, 6), nullable=True),
        sa.Column("highest", sa.Numeric(14, 6), nullable=True),
        sa.Column("chg1h_median", sa.Float(), nullable=True),
        sa.Column("chg24_median", sa.Float(), nullable=True),
        sa.Column("chg24_lowest", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("segment", "gpu", "hour"),
    )
    op.create_index("ix_mgh_hour", "market_gpu_hourly", ["hour"])

    op.create_table(
        "event_detector_state",
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("info", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )

    # Feed-outage detection scans recent fetches across all providers.
    op.execute("CREATE INDEX IF NOT EXISTS ix_raw_fetched_at ON raw_snapshots (fetched_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_raw_fetched_at")
    op.drop_table("event_detector_state")
    op.drop_index("ix_mgh_hour", table_name="market_gpu_hourly")
    op.drop_table("market_gpu_hourly")
    for ix in ("ix_me_type_occurred", "ix_me_provider_occurred", "ix_me_gpu_occurred", "ix_me_occurred"):
        op.drop_index(ix, table_name="market_events")
    op.drop_table("market_events")
