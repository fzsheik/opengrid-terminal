"""lifecycle: provider_resources (per-deployment SSH keys), ops_alert_state (cost-exposure alerts),
deployment_watch deadline columns (store/reconcile.py)

Revision ID: 0015_lifecycle
Revises: 0014_limits
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0015_lifecycle"
down_revision: Union[str, Sequence[str], None] = "0014_limits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "provider_resources",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("provider_resource_id", sa.String(128), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("deployment_id", sa.String(32), nullable=True),
        sa.Column("credential_ref", sa.String(64), nullable=False, server_default=""),
        sa.Column("fingerprint", sa.String(128), nullable=True),
        sa.Column("recorded_by", sa.String(32), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("registered_at", TS, nullable=True),
        sa.Column("delete_requested_at", TS, nullable=True),
        sa.Column("deleted_at", TS, nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("delete_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_delete_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "credential_ref", "resource_type", "name", name="uq_provider_resource_name"),
    )
    op.create_index("ix_provider_resources_status", "provider_resources", ["status"])
    op.create_index("ix_provider_resources_dep", "provider_resources", ["deployment_id"])

    op.create_table(
        "ops_alert_state",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("subject", sa.String(160), nullable=False),
        sa.Column("deployment_id", sa.String(32), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("first_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("last_sent_at", TS, nullable=True),
        sa.Column("sent_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("est_hourly_exposure_usd", sa.Numeric(14, 6), nullable=True),
        sa.Column("payload", J, nullable=True),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("resolution", J, nullable=True),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "subject", name="uq_ops_alert_kind_subject"),
    )
    op.create_index("ix_ops_alert_status", "ops_alert_state", ["status"])

    op.add_column("deployment_watch", sa.Column("past_deadline_at", TS, nullable=True))
    op.add_column("deployment_watch", sa.Column("deadline_retries", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("deployment_watch", "deadline_retries")
    op.drop_column("deployment_watch", "past_deadline_at")
    op.drop_index("ix_ops_alert_status", table_name="ops_alert_state")
    op.drop_table("ops_alert_state")
    op.drop_index("ix_provider_resources_dep", table_name="provider_resources")
    op.drop_index("ix_provider_resources_status", table_name="provider_resources")
    op.drop_table("provider_resources")
