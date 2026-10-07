"""routing, deployments, audit, transactions (store/routing.py)

Revision ID: 0008_routing
Revises: 0007_accounts
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0008_routing"
down_revision: Union[str, Sequence[str], None] = "0007_accounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(14, 6)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "route_requests",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("principal_kind", sa.String(16), nullable=False),
        sa.Column("preview", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_rr_account_time", "route_requests", ["account_id", "created_at"])
    op.create_index("ix_rr_time", "route_requests", ["created_at"])

    op.create_table(
        "routing_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("route_request_id", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(24), nullable=False),
        sa.Column("weights", JSONB, nullable=False),
        sa.Column("candidates", JSONB, nullable=False),
        sa.Column("multi_instance", JSONB, nullable=True),
        sa.Column("exclusions", JSONB, nullable=False),
        sa.Column("selected_provider", sa.String(64), nullable=True),
        sa.Column("selected_listing_id", sa.String(256), nullable=True),
        sa.Column("selected_observed_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("market_snapshot", JSONB, nullable=False),
        sa.Column("methodology_version", sa.String(16), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_rd_request", "routing_decisions", ["route_request_id"])

    op.create_table(
        "deployments",
        sa.Column("deployment_id", sa.String(32), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("route_request_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("listing_id", sa.String(256), nullable=True),
        sa.Column("provider_instance_id", sa.String(128), nullable=True),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("observed_market_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("list_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("quoted_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("quote_basis", sa.String(32), nullable=True),
        sa.Column("actual_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provider_status", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("provisioned_at", TS, nullable=True),
        sa.Column("running_since", TS, nullable=True),
        sa.Column("terminated_at", TS, nullable=True),
        sa.Column("last_checked_at", TS, nullable=True),
        sa.Column("uptime_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interruptions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("termination_reason", sa.String(64), nullable=True),
        sa.Column("credential_source", sa.String(16), nullable=True),
        sa.Column("launch", JSONB, nullable=True),
        sa.Column("provider_metadata", JSONB, nullable=True),
    )
    op.create_index("ix_dep_account_time", "deployments", ["account_id", "created_at"])
    op.create_index("ix_dep_status", "deployments", ["status"])
    op.create_index("ix_dep_request", "deployments", ["route_request_id"])

    op.create_table(
        "deployment_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.String(32), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("from_status", sa.String(16), nullable=True),
        sa.Column("to_status", sa.String(16), nullable=False),
        sa.Column("detail", JSONB, nullable=True),
    )
    op.create_index("ix_depev_dep_time", "deployment_events", ["deployment_id", "at"])

    op.create_table(
        "provision_attempts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("deployment_id", sa.String(32), nullable=False),
        sa.Column("route_request_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("listing_id", sa.String(256), nullable=True),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error_kind", sa.String(24), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_pa_dep", "provision_attempts", ["deployment_id"])
    op.create_index("ix_pa_provider_time", "provision_attempts", ["provider", "started_at"])

    op.create_table(
        "execution_records",
        sa.Column("deployment_id", sa.String(32), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("route_request_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("observed_market_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("quoted_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("actual_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("provision_ok", sa.Boolean(), nullable=False),
        sa.Column("provision_latency_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("uptime_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("interruptions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("termination_reason", sa.String(64), nullable=True),
        sa.Column("workload_completed", sa.Boolean(), nullable=True),
        sa.Column("cost_basis", sa.String(16), nullable=True),
        sa.Column("provider_cost_usd", sa.Numeric(14, 4), nullable=True),
        sa.Column("usage_record_id", sa.Integer(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_exec_provider_time", "execution_records", ["provider", "created_at"])
    op.create_index("ix_exec_account", "execution_records", ["account_id"])


def downgrade() -> None:
    for t in ("execution_records", "provision_attempts", "deployment_events", "deployments",
              "routing_decisions", "route_requests"):
        op.drop_table(t)  # drops its indexes with it
