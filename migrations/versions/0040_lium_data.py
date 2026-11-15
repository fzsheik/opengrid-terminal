"""lium data: Lium's data-partner trade feeds and public market feeds, raw first, internal by default
(lium_data/; store/lium.py; methodology/lium-data.md). Additive only: seven new tables, nothing else changes.

Revision ID: 0040_lium_data
Revises: 0039_monthly_reports
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0040_lium_data"
down_revision: Union[str, Sequence[str], None] = "0039_monthly_reports"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
VIS = "visibility IN ('public', 'internal')"
TABLES = ("lium_model_stats_daily", "lium_throughput_daily", "lium_feed_values", "lium_daily_aggregate",
          "lium_hourly_gpu", "lium_trades", "lium_feed_snapshots")


def _vis(table: str):
    return (sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
            sa.CheckConstraint(VIS, name=f"ck_{table}_visibility"))


def _snap(ondelete: str = "SET NULL", nullable: bool = True):
    return sa.Column("snapshot_id", sa.BigInteger, sa.ForeignKey("lium_feed_snapshots.id", ondelete=ondelete),
                     nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "lium_feed_snapshots",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("feed", sa.String(48), nullable=False),
        sa.Column("url", sa.String(400), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("params_key", sa.String(200), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("seen_count", sa.Integer, nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("body", sa.Text),
        sa.Column("rows", sa.Integer),
        sa.Column("rejected", sa.Integer),
        *_vis("lium_feed_snapshots"),
    )
    op.create_index("ix_lium_feed_snapshots_feed_time", "lium_feed_snapshots", ["feed", "fetched_at"])

    op.create_table(
        "lium_trades",
        sa.Column("row_key", sa.String(64), primary_key=True),
        sa.Column("trade_id", sa.String(128)),
        sa.Column("renter_hash", sa.String(128)),
        sa.Column("provider_hash", sa.String(128)),
        sa.Column("gpu_model", sa.String(128), nullable=False),
        sa.Column("gpu_count", sa.Integer, nullable=False),
        sa.Column("price_per_gpu_hour", sa.Numeric(14, 6), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("start_at", TS, nullable=False),
        sa.Column("end_at", TS, nullable=False),
        sa.Column("gpu_hours", sa.Numeric(16, 6), nullable=False),
        sa.Column("provider_country", sa.String(64)),
        sa.Column("extra", postgresql.JSONB()),
        _snap(),
        sa.Column("first_seen_at", TS, nullable=False),
        *_vis("lium_trades"),
    )
    op.create_index("ix_lium_trades_end", "lium_trades", ["end_at"])
    op.create_index("ix_lium_trades_gpu_end", "lium_trades", ["gpu_model", "end_at"])

    op.create_table(
        "lium_hourly_gpu",
        sa.Column("hour_utc", TS, primary_key=True),
        sa.Column("gpu_model", sa.String(128), primary_key=True),
        sa.Column("rentals", sa.Integer),
        sa.Column("gpu_hours", sa.Numeric(16, 6)),
        sa.Column("mean_price_per_gpu_hour_usd", sa.Numeric(14, 6)),
        _snap(),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_hourly_gpu"),
    )

    op.create_table(
        "lium_daily_aggregate",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("gpu_model", sa.String(128), primary_key=True),
        sa.Column("country", sa.String(64), primary_key=True),
        sa.Column("deals", sa.Integer),
        sa.Column("gpu_hours", sa.Numeric(16, 6)),
        sa.Column("vwap", sa.Numeric(14, 6)),
        sa.Column("low", sa.Numeric(14, 6)),
        sa.Column("high", sa.Numeric(14, 6)),
        sa.Column("extra", postgresql.JSONB()),
        _snap(),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_daily_aggregate"),
    )

    op.create_table(
        "lium_feed_values",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        _snap("CASCADE", nullable=False),
        sa.Column("feed", sa.String(48), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("generated_at", TS),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer, nullable=False),
        sa.Column("metric", sa.String(48), nullable=False),
        sa.Column("value", sa.Numeric(20, 8)),
        sa.Column("value_text", sa.Text),
        sa.Column("value_time", TS),
        sa.Column("present", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("snapshot_id", "subject", "gpu_count", "metric", name="uq_lium_feed_values"),
        *_vis("lium_feed_values"),
    )
    op.create_index("ix_lium_feed_values_feed_time", "lium_feed_values", ["feed", "observed_at"])
    op.create_index("ix_lium_feed_values_key", "lium_feed_values", ["feed", "subject", "gpu_count", "metric", "observed_at"])

    op.create_table(
        "lium_throughput_daily",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("gmv_usd", sa.Numeric(16, 2)),
        sa.Column("gpu_hours", sa.Numeric(16, 3)),
        sa.Column("rentals", sa.Integer),
        sa.Column("utilisation", sa.Numeric(8, 6)),
        sa.Column("utilisation_partial", sa.Boolean),
        sa.Column("raw_snapshot_id", sa.BigInteger),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("revisions", sa.Integer, nullable=False, server_default="0"),
        *_vis("lium_throughput_daily"),
    )

    op.create_table(
        "lium_model_stats_daily",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("gpu_model", sa.String(128), primary_key=True),
        sa.Column("median_price_30d", sa.Numeric(14, 6)),
        sa.Column("utilisation_30d", sa.Numeric(8, 6)),
        sa.Column("from_price", sa.Numeric(14, 6)),
        sa.Column("gpus_listed", sa.Integer),
        sa.Column("gpus_rented", sa.Integer),
        sa.Column("raw_snapshot_id", sa.BigInteger),
        sa.Column("observed_at", TS, nullable=False),
        *_vis("lium_model_stats_daily"),
    )


def downgrade() -> None:
    for t in TABLES:
        op.drop_table(t)
