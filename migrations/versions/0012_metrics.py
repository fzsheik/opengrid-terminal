"""routing quality, reliability, partners, product analytics (store/metrics.py)

Revision ID: 0012_metrics
Revises: 0011_reconcile
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0012_metrics"
down_revision: Union[str, Sequence[str], None] = "0011_reconcile"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(14, 6)
PCT = sa.Numeric(9, 4)
JSONB = postgresql.JSONB()
TEXTS = postgresql.ARRAY(sa.Text())


def upgrade() -> None:
    op.create_table(
        "route_outcomes",
        sa.Column("route_request_id", sa.String(32), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("preview", sa.Boolean(), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("strategy", sa.String(24), nullable=True),
        sa.Column("request_status", sa.String(32), nullable=True),
        sa.Column("winner_provider", sa.String(64), nullable=True),
        sa.Column("winner_listing_id", sa.String(256), nullable=True),
        sa.Column("winner_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("runner_up_provider", sa.String(64), nullable=True),
        sa.Column("runner_up_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("candidates_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates", JSONB, nullable=True),
        sa.Column("market_median_per_gpu_hour", PRICE, nullable=True),
        sa.Column("market_providers", sa.Integer(), nullable=True),
        sa.Column("cheapest_valid_provider", sa.String(64), nullable=True),
        sa.Column("cheapest_valid_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("expected_savings_pct", PCT, nullable=True),
        sa.Column("deployment_id", sa.String(32), nullable=True),
        sa.Column("deployment_provider", sa.String(64), nullable=True),
        sa.Column("deployment_purpose", sa.String(16), nullable=True),
        sa.Column("deployment_status", sa.String(32), nullable=True),
        sa.Column("launched", sa.Boolean(), nullable=True),
        sa.Column("provisioned", sa.Boolean(), nullable=True),
        sa.Column("provisioning_latency_ms", sa.Integer(), nullable=True),
        sa.Column("quoted_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("actual_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("realized_savings_pct", PCT, nullable=True),
        sa.Column("quote_error_pct", PCT, nullable=True),
        sa.Column("interrupted", sa.Boolean(), nullable=True),
        sa.Column("interruptions", sa.Integer(), nullable=True),
        sa.Column("uptime_seconds", sa.Integer(), nullable=True),
        sa.Column("gpu_hours", sa.Numeric(14, 6), nullable=True),
        sa.Column("comparison_valid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("comparison_reason", sa.Text(), nullable=True),
        sa.Column("final", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("computed_at", TS, nullable=False),
    )
    op.create_index("ix_ro_created", "route_outcomes", ["created_at"])
    op.create_index("ix_ro_account_created", "route_outcomes", ["account_id", "created_at"])
    op.create_index("ix_ro_provider", "route_outcomes", ["winner_provider"])
    op.create_index("ix_ro_open", "route_outcomes", ["final"])

    op.create_table(
        "partner_profiles",
        sa.Column("account_id", sa.Integer(), primary_key=True),
        sa.Column("company", sa.String(200), nullable=False),
        sa.Column("technical_contact_name", sa.String(200), nullable=True),
        sa.Column("technical_contact_email", sa.String(320), nullable=True),
        sa.Column("preferred_gpus", TEXTS, nullable=False, server_default="{}"),
        sa.Column("regions", TEXTS, nullable=False, server_default="{}"),
        sa.Column("workload_type", sa.String(64), nullable=True),
        sa.Column("normal_provider", sa.String(64), nullable=True),
        sa.Column("normal_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("max_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("expected_gpu_count", sa.Integer(), nullable=True),
        sa.Column("expected_duration_hours", sa.Numeric(10, 2), nullable=True),
        sa.Column("latency_requirements", sa.Text(), nullable=True),
        sa.Column("storage_requirements", sa.Text(), nullable=True),
        sa.Column("network_requirements", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="invited"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=True),
    )

    op.create_table(
        "deployment_feedback",
        sa.Column("deployment_id", sa.String(32), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("would_have_chosen_provider", sa.Boolean(), nullable=True),
        sa.Column("price_better", sa.Boolean(), nullable=True),
        sa.Column("setup_easier", sa.Boolean(), nullable=True),
        sa.Column("would_route_next", sa.Boolean(), nullable=True),
        sa.Column("what_broke", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=True),
    )
    op.create_index("ix_feedback_account", "deployment_feedback", ["account_id", "created_at"])

    op.create_table(
        "product_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", TS, nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("anon_id", sa.String(64), nullable=True),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("props", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("source", sa.String(8), nullable=False, server_default="client"),
        sa.Column("dedupe_key", sa.String(128), nullable=True),
    )
    op.create_index("ix_pe_ts", "product_events", ["ts"])
    op.create_index("ix_pe_event_ts", "product_events", ["event", "ts"])
    op.create_index("ix_pe_anon_ts", "product_events", ["anon_id", "ts"])
    op.create_index("ix_pe_account_ts", "product_events", ["account_id", "ts"])
    op.create_index("ux_pe_dedupe", "product_events", ["dedupe_key"], unique=True)


def downgrade() -> None:
    for name, table in (("ux_pe_dedupe", "product_events"), ("ix_pe_account_ts", "product_events"),
                        ("ix_pe_anon_ts", "product_events"), ("ix_pe_event_ts", "product_events"),
                        ("ix_pe_ts", "product_events")):
        op.drop_index(name, table_name=table)
    op.drop_table("product_events")
    op.drop_index("ix_feedback_account", table_name="deployment_feedback")
    op.drop_table("deployment_feedback")
    op.drop_table("partner_profiles")
    for name in ("ix_ro_open", "ix_ro_provider", "ix_ro_account_created", "ix_ro_created"):
        op.drop_index(name, table_name="route_outcomes")
    op.drop_table("route_outcomes")
