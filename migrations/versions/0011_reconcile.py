"""reconciliation runs, orphan resources, deployment watch, usage slices (store/reconcile.py)

Revision ID: 0011_reconcile
Revises: 0010_execution
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0011_reconcile"
down_revision: Union[str, Sequence[str], None] = "0010_execution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("providers", J, nullable=True),
        sa.Column("findings", J, nullable=True),
        sa.Column("counts", J, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_recon_runs_started", "reconciliation_runs", ["started_at"])

    op.create_table(
        "orphan_resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("instance_name", sa.String(255), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("deployment_id", sa.String(32), nullable=True),
        sa.Column("credential_ref", sa.String(64), nullable=True),
        sa.Column("provider_state", sa.String(32), nullable=True),
        sa.Column("provider_status", sa.String(64), nullable=True),
        sa.Column("price_per_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("seen_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("provably_ours", sa.Boolean(), nullable=False),
        sa.Column("auto_terminated", sa.Boolean(), nullable=False),
        sa.Column("terminate_attempts", sa.Integer(), nullable=False),
        sa.Column("last_terminate_at", TS, nullable=True),
        sa.Column("last_terminate_outcome", sa.String(16), nullable=True),
        sa.Column("action", sa.String(16), nullable=True),
        sa.Column("resolved_by", sa.String(64), nullable=True),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "instance_id", name="uq_orphan_provider_instance"),
    )
    op.create_index("ix_orphan_status", "orphan_resources", ["status"])

    op.create_table(
        "deployment_watch",
        sa.Column("deployment_id", sa.String(32), nullable=False),
        sa.Column("last_observed_at", TS, nullable=True),
        sa.Column("last_observed_state", sa.String(16), nullable=True),
        sa.Column("last_provider_status", sa.String(64), nullable=True),
        sa.Column("last_running_at", TS, nullable=True),
        sa.Column("consecutive_errors", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("not_found_first_at", TS, nullable=True),
        sa.Column("not_found_count", sa.Integer(), nullable=False),
        sa.Column("terminate_attempts", sa.Integer(), nullable=False),
        sa.Column("last_terminate_at", TS, nullable=True),
        sa.Column("next_terminate_at", TS, nullable=True),
        sa.Column("last_terminate_outcome", sa.String(16), nullable=True),
        sa.Column("credentials_unavailable_at", TS, nullable=True),
        sa.Column("ended_at", TS, nullable=True),
        sa.Column("ended_basis", sa.String(32), nullable=True),
        sa.Column("metered_through", TS, nullable=True),
        sa.Column("metering_complete", sa.Boolean(), nullable=False),
        sa.Column("alerts", J, nullable=True),
        sa.Column("validation", J, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("deployment_id"),
    )

    op.create_table(
        "usage_slices",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("deployment_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("period_start", TS, nullable=False),
        sa.Column("period_end", TS, nullable=False),
        sa.Column("running_seconds", sa.Integer(), nullable=False),
        sa.Column("stopped_seconds", sa.Integer(), nullable=False),
        sa.Column("unbilled_seconds", sa.Integer(), nullable=False),
        sa.Column("billable_seconds", sa.Integer(), nullable=False),
        sa.Column("stopped_billing", sa.String(16), nullable=True),
        sa.Column("price_per_gpu_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("price_basis", sa.String(16), nullable=True),
        sa.Column("cost_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("usage_record_id", sa.Integer(), nullable=True),
        sa.Column("end_estimated", sa.Boolean(), nullable=False),
        sa.Column("final", sa.Boolean(), nullable=False),
        sa.Column("detail", J, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("deployment_id", "period_start", name="uq_usage_slice_dep_period"),
    )
    op.create_index("ix_usage_slices_account_period", "usage_slices", ["account_id", "period_start"])


def downgrade() -> None:
    op.drop_index("ix_usage_slices_account_period", table_name="usage_slices")
    op.drop_table("usage_slices")
    op.drop_table("deployment_watch")
    op.drop_index("ix_orphan_status", table_name="orphan_resources")
    op.drop_table("orphan_resources")
    op.drop_index("ix_recon_runs_started", table_name="reconciliation_runs")
    op.drop_table("reconciliation_runs")
