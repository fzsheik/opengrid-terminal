"""uptime: external control-plane probe data from UptimeProject (uptime/; methodology/uptime.md). Additive only.

    uptime_services       services of providers OpenGrid tracks, with headline availability and status
    uptime_hourly         hourly availability / down + degraded minutes / p50 latency per service
    uptime_daily          daily buckets (the only granularity UptimeProject serves beyond 7 days; used to backfill)
    uptime_probe_samples  per-probe status + latency at each poll
    uptime_fetch_log      one row per poll

Revision ID: 0022_uptime
Revises: 0021_job_relaunch
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0022_uptime"
down_revision: Union[str, Sequence[str], None] = "0021_job_relaunch"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "uptime_services",
        sa.Column("id", sa.String(96), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("upstream_provider", sa.String(200), nullable=True),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("homepage_url", sa.Text(), nullable=True),
        sa.Column("status_page_url", sa.Text(), nullable=True),
        sa.Column("current_status", sa.String(16), nullable=True),
        sa.Column("checks_count", sa.Integer(), nullable=True),
        sa.Column("checks", J, nullable=True),
        sa.Column("availability_24h", sa.Numeric(9, 6), nullable=True),
        sa.Column("availability_7d", sa.Numeric(9, 6), nullable=True),
        sa.Column("availability_30d", sa.Numeric(9, 6), nullable=True),
        sa.Column("availability_90d", sa.Numeric(9, 6), nullable=True),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
    )
    op.create_index("ix_uptime_services_provider", "uptime_services", ["provider"])
    op.create_table(
        "uptime_hourly",
        sa.Column("service_id", sa.String(96), nullable=False),
        sa.Column("bucket", TS, nullable=False),
        sa.Column("availability_pct", sa.Numeric(9, 6), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("down_minutes", sa.Integer(), nullable=True),
        sa.Column("degraded_minutes", sa.Integer(), nullable=True),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("service_id", "bucket"),
    )
    op.create_index("ix_uptime_hourly_bucket", "uptime_hourly", ["bucket"])
    op.create_table(
        "uptime_daily",
        sa.Column("service_id", sa.String(96), nullable=False),
        sa.Column("bucket", TS, nullable=False),
        sa.Column("availability_pct", sa.Numeric(9, 6), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("down_minutes", sa.Integer(), nullable=True),
        sa.Column("degraded_minutes", sa.Integer(), nullable=True),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("service_id", "bucket"),
    )
    op.create_table(
        "uptime_probe_samples",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.String(96), nullable=False),
        sa.Column("probe_id", sa.String(32), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_uptime_probe_service_time", "uptime_probe_samples", ["service_id", "observed_at"])
    op.create_table(
        "uptime_fetch_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("services", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_uptime_fetch_log_fetched_at", "uptime_fetch_log", ["fetched_at"])


def downgrade() -> None:
    op.drop_table("uptime_fetch_log")
    op.drop_table("uptime_probe_samples")
    op.drop_table("uptime_daily")
    op.drop_table("uptime_hourly")
    op.drop_table("uptime_services")
