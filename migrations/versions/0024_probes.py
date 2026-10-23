"""probes: OpenGrid's own provider probes, raw first (probes/; methodology/probes.md). Additive only.

    probe_results          every probe result untouched: target, vantage point, time, ok, status, classified error,
                           DNS / TCP / TLS / TTFB / total timings
    probe_agents           the vantage points that have reported, with when they last did
    probe_location_hourly  per target, vantage point, hour: checks, ok, latency p50 / p95 / max, error classes
    probe_target_hourly    per target, hour: slots by class (ok / regional / global / single), availability, failing
                           vantage points, error classes. Both hourly tables are derived and can be rebuilt from the raw.

Revision ID: 0024_probes
Revises: 0023_incidents
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0024_probes"
down_revision: Union[str, Sequence[str], None] = "0023_incidents"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "probe_results",
        sa.Column("target_id", sa.String(96), nullable=False),
        sa.Column("location", sa.String(32), nullable=False),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("agent_id", sa.String(48), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error_class", sa.String(24), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dns_ms", sa.Integer(), nullable=True),
        sa.Column("connect_ms", sa.Integer(), nullable=True),
        sa.Column("tls_ms", sa.Integer(), nullable=True),
        sa.Column("ttfb_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("resolved_ip", sa.String(64), nullable=True),
        sa.Column("received_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("target_id", "location", "observed_at"),
    )
    op.create_index("ix_probe_results_time", "probe_results", ["observed_at"])
    op.create_table(
        "probe_agents",
        sa.Column("agent_id", sa.String(48), primary_key=True),
        sa.Column("location", sa.String(32), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("results_total", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "probe_location_hourly",
        sa.Column("target_id", sa.String(96), nullable=False),
        sa.Column("location", sa.String(32), nullable=False),
        sa.Column("bucket", TS, nullable=False),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("ok_checks", sa.Integer(), nullable=False),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("latency_p95_ms", sa.Integer(), nullable=True),
        sa.Column("latency_max_ms", sa.Integer(), nullable=True),
        sa.Column("error_classes", J, nullable=False, server_default="{}"),
        sa.PrimaryKeyConstraint("target_id", "location", "bucket"),
    )
    op.create_table(
        "probe_target_hourly",
        sa.Column("target_id", sa.String(96), nullable=False),
        sa.Column("bucket", TS, nullable=False),
        sa.Column("checks", sa.Integer(), nullable=False),
        sa.Column("ok_checks", sa.Integer(), nullable=False),
        sa.Column("availability", sa.Numeric(9, 6), nullable=True),
        sa.Column("slots_ok", sa.Integer(), nullable=False),
        sa.Column("slots_regional", sa.Integer(), nullable=False),
        sa.Column("slots_global", sa.Integer(), nullable=False),
        sa.Column("slots_single", sa.Integer(), nullable=False),
        sa.Column("slot_minutes", sa.Integer(), nullable=False),
        sa.Column("failing_locations", J, nullable=False, server_default="{}"),
        sa.Column("error_classes", J, nullable=False, server_default="{}"),
        sa.Column("locations", J, nullable=False, server_default="[]"),
        sa.Column("latency_p50_ms", sa.Integer(), nullable=True),
        sa.Column("computed_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("target_id", "bucket"),
    )
    op.create_index("ix_probe_target_hourly_bucket", "probe_target_hourly", ["bucket"])


def downgrade() -> None:
    op.drop_table("probe_target_hourly")
    op.drop_table("probe_location_hourly")
    op.drop_table("probe_agents")
    op.drop_table("probe_results")
