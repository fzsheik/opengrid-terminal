"""power markets: wholesale power prices and grid data in their own tables (power/; methodology/power-markets.md).
Additive only: no existing table is touched.

    power_fetches            every request to a power source (url, status, bytes, sha256; the bytes where small)
    power_series             the series catalogue (hub / zone / balancing-authority series; electricity and
                             natural gas), visibility per terms
    power_days               one row per (series, market day): the day's values as real[] + mean / min / max
    power_gpu_region_daily   DERIVED: power cost per GPU-hour and the spread to the GPU price, per region and day
    power_leadlag            DERIVED: lagged correlations of GPU price / capacity changes with power price changes

Revision ID: 0035_power_markets
Revises: 0029_news_history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0035_power_markets"
down_revision: Union[str, Sequence[str], None] = "0034_api_key_version"  # re-chained after Data API v1 at merge
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "power_fetches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("empty", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("content_type", sa.String(80), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("encoding", sa.String(8), nullable=True),
        sa.Column("body", sa.LargeBinary(), nullable=True),
        sa.Column("days_stored", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_power_fetches_dataset_period", "power_fetches", ["dataset", "period_start"])
    op.create_table(
        "power_series",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key", sa.String(120), nullable=False, unique=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("iso", sa.String(16), nullable=True),
        sa.Column("commodity", sa.String(16), nullable=False, server_default="electricity"),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("market", sa.String(16), nullable=False),
        sa.Column("location", sa.String(64), nullable=False),
        sa.Column("location_type", sa.String(16), nullable=False),
        sa.Column("unit", sa.String(24), nullable=False),
        sa.Column("interval_minutes", sa.Integer(), nullable=False),
        sa.Column("tz", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "power_days",
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("power_series.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("vals", postgresql.ARRAY(sa.REAL()), nullable=False),
        sa.Column("n", sa.SmallInteger(), nullable=False),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("mean", sa.REAL(), nullable=True),
        sa.Column("min", sa.REAL(), nullable=True),
        sa.Column("max", sa.REAL(), nullable=True),
        sa.Column("fetch_id", sa.BigInteger(), sa.ForeignKey("power_fetches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("revisions", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.PrimaryKeyConstraint("series_id", "day"),
    )
    op.create_index("ix_power_days_day", "power_days", ["day"])
    op.create_table(
        "power_gpu_region_daily",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("region", sa.String(128), nullable=False),
        sa.Column("gpu", sa.String(128), nullable=False),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("iso", sa.String(16), nullable=False),
        sa.Column("price_series", sa.String(120), nullable=False),
        sa.Column("match", sa.String(24), nullable=False),
        sa.Column("power_price_usd_mwh", sa.Float(), nullable=False),
        sa.Column("power_price_max_usd_mwh", sa.Float(), nullable=True),
        sa.Column("tdp_w", sa.Integer(), nullable=False),
        sa.Column("pue", sa.Float(), nullable=False),
        sa.Column("power_cost_usd_per_gpu_hour", sa.Float(), nullable=False),
        sa.Column("gpu_price_median", sa.Float(), nullable=True),
        sa.Column("gpu_price_low", sa.Float(), nullable=True),
        sa.Column("listings", sa.Integer(), nullable=False),
        sa.Column("spread_usd_per_gpu_hour", sa.Float(), nullable=True),
        sa.Column("power_share", sa.Float(), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("computed_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("day", "provider", "region", "gpu"),
    )
    op.create_index("ix_power_gpu_region_daily_gpu_day", "power_gpu_region_daily", ["gpu", "day"])
    op.create_table(
        "power_leadlag",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("gpu", sa.String(128), nullable=False),
        sa.Column("iso", sa.String(16), nullable=False),
        sa.Column("price_series", sa.String(120), nullable=False),
        sa.Column("metric", sa.String(32), nullable=False),
        sa.Column("lag_days", sa.SmallInteger(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column("corr", sa.Float(), nullable=True),
        sa.Column("spike_days", sa.Integer(), nullable=False),
        sa.Column("visibility", sa.String(16), nullable=False),
        sa.Column("computed_at", TS, nullable=False),
        sa.Column("method", sa.Text(), nullable=False),
        sa.UniqueConstraint("gpu", "iso", "price_series", "metric", "lag_days", "window_end", name="uq_power_leadlag"),
    )


def downgrade() -> None:
    op.drop_table("power_leadlag")
    op.drop_index("ix_power_gpu_region_daily_gpu_day", table_name="power_gpu_region_daily")
    op.drop_table("power_gpu_region_daily")
    op.drop_index("ix_power_days_day", table_name="power_days")
    op.drop_table("power_days")
    op.drop_table("power_series")
    op.drop_index("ix_power_fetches_dataset_period", table_name="power_fetches")
    op.drop_table("power_fetches")
