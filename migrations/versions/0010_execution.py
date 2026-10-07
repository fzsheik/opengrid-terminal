"""execution control plane, quotes, idempotency, state machine, limits (store/routing.py)

Revision ID: 0010_execution
Revises: 0009_frontend

Additive. Production may already hold 0008-era deployments; their statuses are mapped onto the new state
machine (routing/deployments.py) so nothing that may still exist at a provider is treated as final:

    pending / routing (no instance id)   -> launch_unknown   a provision call may have been in flight
    provisioning / running / stopped / terminating / terminated  -> unchanged
    failed, no instance id, no reconciliation flag               -> provision_failed
    failed, no instance id, needs_reconciliation (timeout)       -> launch_unknown
    failed WITH an instance id (provider-reported failure)       -> launch_unknown (reconciliation resolves it)

credential_ref is back-filled: 'platform:<provider>' for OpenGrid-managed, and for BYO the account's BYO row that
was active at the deployment's creation ('byo:<id>'), else 'byo:unknown' (-> credentials_unavailable on use, by
design: never guess another key). client_name is the legacy 'opengrid-<deployment_id>' name those launches used.
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0010_execution"
down_revision: Union[str, Sequence[str], None] = "0009_frontend"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
PRICE = sa.Numeric(14, 6)
JSONB = postgresql.JSONB()


def upgrade() -> None:
    op.create_table(
        "execution_controls",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_table(
        "execution_control_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("target", sa.String(128), nullable=True),
        sa.Column("before", JSONB, nullable=True),
        sa.Column("after", JSONB, nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(64), nullable=True),
    )
    op.create_index("ix_ecl_time", "execution_control_log", ["at"])
    op.create_index("ix_ecl_target", "execution_control_log", ["target"])
    op.create_table(
        "provider_execution_flags",
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column("adapter_status", sa.String(16), nullable=False, server_default="simulated"),
        sa.Column("validated_at", TS, nullable=True),
        sa.Column("validation_deployment_id", sa.String(32), nullable=True),
        sa.Column("validation_evidence", JSONB, nullable=True),
        sa.Column("supervised_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("live_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("killed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("kill_reason", sa.Text(), nullable=True),
        sa.Column("killed_at", TS, nullable=True),
        sa.Column("killed_by", sa.String(64), nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )
    op.create_table(
        "quotes",
        sa.Column("id", sa.String(40), primary_key=True),
        sa.Column("route_request_id", sa.String(32), nullable=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("listing_id", sa.String(256), nullable=False),
        sa.Column("offer", JSONB, nullable=False),
        sa.Column("availability", JSONB, nullable=True),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("region_group", sa.String(32), nullable=True),
        sa.Column("observed_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("quote_price_per_gpu_hour", PRICE, nullable=False),
        sa.Column("est_hourly_cost", sa.Numeric(14, 4), nullable=False),
        sa.Column("est_total_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("duration_hours", sa.Numeric(10, 3), nullable=True),
        sa.Column("fees", JSONB, nullable=True),
        sa.Column("taxes", JSONB, nullable=True),
        sa.Column("billing_unit", sa.String(64), nullable=True),
        sa.Column("minimum_commitment", sa.String(128), nullable=True),
        sa.Column("price_source", sa.String(16), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False, server_default="customer"),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("consumed_by_deployment_id", sa.String(32), nullable=True),
        sa.Column("superseded_by", sa.String(40), nullable=True),
    )
    op.create_index("ix_quotes_rr", "quotes", ["route_request_id"])
    op.create_index("ix_quotes_account_time", "quotes", ["account_id", "created_at"])
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("principal", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_code", sa.Integer(), nullable=True),
        sa.Column("response", JSONB, nullable=True),
        sa.Column("resource_id", sa.String(64), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.UniqueConstraint("principal", "scope", "key", name="ux_idem_principal_scope_key"),
    )
    op.create_index("ix_idem_expires", "idempotency_keys", ["expires_at"])
    op.create_table(
        "account_limits",
        sa.Column("account_id", sa.Integer(), primary_key=True),
        sa.Column("max_price_per_gpu_hour", PRICE, nullable=True),
        sa.Column("max_hourly_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("max_total_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("max_gpus", sa.Integer(), nullable=True),
        sa.Column("max_active_deployments", sa.Integer(), nullable=True),
        sa.Column("provider_allowlist", postgresql.ARRAY(sa.String(64)), nullable=True),
        sa.Column("region_allowlist", postgresql.ARRAY(sa.String(64)), nullable=True),
        sa.Column("monthly_spend_limit", sa.Numeric(14, 2), nullable=True),
        sa.Column("updated_at", TS, nullable=True),
        sa.Column("updated_by", sa.String(64), nullable=True),
    )

    # --- deployments: wider status, new columns ------------------------------------------------
    op.alter_column("deployments", "status", type_=sa.String(32), existing_type=sa.String(16), existing_nullable=False)
    for name, typ in [
        ("purpose", sa.String(16)), ("quote_id", sa.String(40)), ("approved_by", sa.String(64)),
        ("approved_at", TS), ("approval_mode", sa.String(16)), ("max_runtime_minutes", sa.Integer()),
        ("terminate_deadline_at", TS), ("launch_token", sa.String(64)), ("client_name", sa.String(80)),
        ("credential_ref", sa.String(64)), ("credential_account_id", sa.Integer()), ("limit_violations", JSONB),
        ("override_reason", sa.Text()), ("state_changed_at", TS), ("terminate_requested_at", TS),
        ("provider_reported_cost", sa.Numeric(14, 4)), ("reconciled_at", TS), ("reconciliation", JSONB),
    ]:
        op.add_column("deployments", sa.Column(name, typ, nullable=True))
    op.add_column("deployments", sa.Column("override_limits", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.execute("UPDATE deployments SET purpose = 'customer' WHERE purpose IS NULL")
    op.alter_column("deployments", "purpose", existing_type=sa.String(16), nullable=False, server_default="customer")
    op.create_index("ux_dep_launch_token", "deployments", ["launch_token"], unique=True)

    # --- deployment_events: wider statuses, actor / reason / evidence ------------------------------
    op.alter_column("deployment_events", "from_status", type_=sa.String(32), existing_type=sa.String(16))
    op.alter_column("deployment_events", "to_status", type_=sa.String(32), existing_type=sa.String(16),
                    existing_nullable=False)
    op.add_column("deployment_events", sa.Column("actor", sa.String(16), nullable=True))
    op.add_column("deployment_events", sa.Column("actor_id", sa.String(64), nullable=True))
    op.add_column("deployment_events", sa.Column("reason", sa.Text(), nullable=True))
    op.add_column("deployment_events", sa.Column("evidence", JSONB, nullable=True))

    # --- provision_attempts: write-ahead row ---------------------------------------------------------
    op.alter_column("provision_attempts", "ok", existing_type=sa.Boolean(), nullable=True)
    for name, typ in [("outcome", sa.String(16)), ("launch_token", sa.String(64)), ("client_name", sa.String(80)),
                      ("credential_ref", sa.String(64)), ("quote_id", sa.String(40)), ("instance_id", sa.String(128)),
                      ("status_code", sa.Integer()), ("provider_request_id", sa.String(128)),
                      ("request_summary", JSONB)]:
        op.add_column("provision_attempts", sa.Column(name, typ, nullable=True))
    op.create_index("ux_pa_launch_token", "provision_attempts", ["launch_token"], unique=True)
    op.create_index("ix_pa_outcome", "provision_attempts", ["outcome"])

    # --- map existing rows onto the new machine (see docstring) ------------------------------------
    op.execute("""
        UPDATE provision_attempts SET outcome = CASE
            WHEN ok THEN 'accepted'
            WHEN error_kind IN ('timeout', 'unknown_state') THEN 'unknown'
            ELSE 'rejected' END
        WHERE outcome IS NULL
    """)
    op.execute("""
        UPDATE deployments SET client_name = 'opengrid-' || deployment_id WHERE client_name IS NULL
    """)
    op.execute("""
        UPDATE deployments SET credential_ref = 'platform:' || CASE
            WHEN provider IN ('crusoe', 'denvr', 'latitude') THEN 'shadeform' ELSE provider END
        WHERE credential_source = 'opengrid' AND credential_ref IS NULL AND provider IS NOT NULL
    """)
    op.execute("""
        UPDATE deployments d SET credential_ref = COALESCE((
            SELECT 'byo:' || c.id FROM provider_credentials c
            WHERE c.account_id = d.account_id
              AND c.provider = CASE WHEN d.provider IN ('crusoe', 'denvr', 'latitude') THEN 'shadeform' ELSE d.provider END
              AND c.created_at <= d.created_at AND (c.revoked_at IS NULL OR c.revoked_at > d.created_at)
            ORDER BY c.id DESC LIMIT 1), 'byo:unknown'),
            credential_account_id = d.account_id
        WHERE d.credential_source = 'byo' AND d.credential_ref IS NULL
    """)
    op.execute("""
        INSERT INTO deployment_events (deployment_id, at, from_status, to_status, actor, reason)
        SELECT deployment_id, now(), status,
               CASE WHEN status IN ('pending', 'routing') THEN 'launch_unknown'
                    WHEN provider_instance_id IS NOT NULL THEN 'launch_unknown'
                    WHEN COALESCE((provider_metadata ->> 'needs_reconciliation')::boolean, false) THEN 'launch_unknown'
                    ELSE 'provision_failed' END,
               'system', 'migration 0010_execution: legacy status mapped onto the execution state machine'
        FROM deployments WHERE status IN ('pending', 'routing', 'failed')
    """)
    op.execute("""
        UPDATE deployments SET status = CASE
                WHEN status IN ('pending', 'routing') THEN 'launch_unknown'
                WHEN provider_instance_id IS NOT NULL THEN 'launch_unknown'
                WHEN COALESCE((provider_metadata ->> 'needs_reconciliation')::boolean, false) THEN 'launch_unknown'
                ELSE 'provision_failed' END,
            state_changed_at = now()
        WHERE status IN ('pending', 'routing', 'failed')
    """)


_LEGACY = """
    UPDATE deployments SET status = CASE
        WHEN status IN ('created', 'quoted', 'pending_approval', 'approved', 'quote_expired') THEN 'pending'
        WHEN status IN ('degraded', 'orphan_suspected', 'credentials_unavailable', 'provider_timeout',
                        'launch_unknown') THEN CASE WHEN provider_instance_id IS NULL THEN 'failed' ELSE 'running' END
        WHEN status = 'stopping' THEN 'running'
        WHEN status = 'termination_failed' THEN 'terminating'
        WHEN status IN ('quote_failed', 'rejected', 'provision_failed', 'provider_rejected') THEN 'failed'
        ELSE status END
"""


def downgrade() -> None:
    op.execute(_LEGACY)
    op.execute("DELETE FROM deployment_events WHERE length(to_status) > 16 OR length(coalesce(from_status, '')) > 16")
    op.drop_index("ix_pa_outcome", table_name="provision_attempts")
    op.drop_index("ux_pa_launch_token", table_name="provision_attempts")
    for name in ("outcome", "launch_token", "client_name", "credential_ref", "quote_id", "instance_id", "status_code",
                 "provider_request_id", "request_summary"):
        op.drop_column("provision_attempts", name)
    op.execute("UPDATE provision_attempts SET ok = false WHERE ok IS NULL")
    op.alter_column("provision_attempts", "ok", existing_type=sa.Boolean(), nullable=False)
    for name in ("actor", "actor_id", "reason", "evidence"):
        op.drop_column("deployment_events", name)
    op.alter_column("deployment_events", "to_status", type_=sa.String(16), existing_type=sa.String(32),
                    existing_nullable=False)
    op.alter_column("deployment_events", "from_status", type_=sa.String(16), existing_type=sa.String(32))
    op.drop_index("ux_dep_launch_token", table_name="deployments")
    for name in ("purpose", "quote_id", "approved_by", "approved_at", "approval_mode", "max_runtime_minutes",
                 "terminate_deadline_at", "launch_token", "client_name", "credential_ref", "credential_account_id",
                 "limit_violations", "override_reason", "state_changed_at", "terminate_requested_at",
                 "provider_reported_cost", "reconciled_at", "reconciliation", "override_limits"):
        op.drop_column("deployments", name)
    op.alter_column("deployments", "status", type_=sa.String(16), existing_type=sa.String(32), existing_nullable=False)
    for t in ("account_limits", "idempotency_keys", "quotes", "provider_execution_flags", "execution_control_log",
              "execution_controls"):
        op.drop_table(t)
