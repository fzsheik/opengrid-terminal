"""Reconciliation and metering tables (migration 0011_reconcile; methodology/reconciliation.md).

    reconciliation_runs   one row per reconcile pass: when, which providers, every finding (jsonb)
    orphan_resources      provider instances OpenGrid cannot account for: an og-* instance with no live
                          deployment, a deployment ended in OpenGrid while the instance is alive, a duplicate
                          launch. One row per (provider, instance_id); never deleted, only resolved.
    deployment_watch      the reconciler's / tracker's per-deployment memory: the last provider observation,
                          the two-signal not_found evidence, terminate retries with backoff, the provider-
                          confirmed end time, how far usage has been metered
    usage_slices          incremental metering: one row per deployment per UTC hour (period_start unique),
                          running vs stopped vs unbilled seconds, the price and its basis, the stopped-billing
                          rule applied, and the billing usage record it produced

Lifecycle (migration 0015_lifecycle):
    provider_resources    temporary provider-side resources OpenGrid creates besides the instance (per-deployment
                          SSH keys): recorded BEFORE the provider call, deleted after confirmed termination;
                          one row per (provider, credential_ref, resource_type, name)
    ops_alert_state       cost-exposure alerts (alerts/ops.py exposure()): one row per (kind, subject), open until
                          resolved WITH evidence, re-escalated every settings.alert_reescalate_minutes
    deployment_watch      + past_deadline_at / deadline_retries (deadline enforcement through provider outages)

account_id / deployment_id are plain columns (no cross-domain foreign keys), like the rest of the schema.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (Index("ix_recon_runs_started", "started_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str] = mapped_column(String(16))                 # job | manual | test
    provider: Mapped[str | None] = mapped_column(String(64))         # None = every provider
    status: Mapped[str] = mapped_column(String(16))                  # running | ok | partial | failed
    providers: Mapped[dict | None] = mapped_column(JSONB)            # per provider: listed, errors, skipped reason
    findings: Mapped[list | None] = mapped_column(JSONB)             # [{kind, provider, deployment_id, ...}]
    counts: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)


class OrphanResource(Base):
    __tablename__ = "orphan_resources"
    __table_args__ = (
        UniqueConstraint("provider", "instance_id", name="uq_orphan_provider_instance"),
        Index("ix_orphan_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))
    instance_id: Mapped[str] = mapped_column(String(128))
    instance_name: Mapped[str | None] = mapped_column(String(255))
    # og_no_deployment | deployment_ended_alive | duplicate_launch
    kind: Mapped[str] = mapped_column(String(32))
    deployment_id: Mapped[str | None] = mapped_column(String(32))
    credential_ref: Mapped[str | None] = mapped_column(String(64))
    provider_state: Mapped[str | None] = mapped_column(String(32))
    provider_status: Mapped[str | None] = mapped_column(String(64))
    price_per_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)
    # open | terminating | terminated | ignored | adopted | gone
    status: Mapped[str] = mapped_column(String(16))
    provably_ours: Mapped[bool] = mapped_column(Boolean, default=False)   # og-* name AND a terminal deployment
    auto_terminated: Mapped[bool] = mapped_column(Boolean, default=False)
    terminate_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_terminate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_terminate_outcome: Mapped[str | None] = mapped_column(String(16))
    action: Mapped[str | None] = mapped_column(String(16))               # terminate | ignore | adopt (operator)
    resolved_by: Mapped[str | None] = mapped_column(String(64))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeploymentWatch(Base):
    __tablename__ = "deployment_watch"

    deployment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # last successful read
    last_observed_state: Mapped[str | None] = mapped_column(String(16))
    last_provider_status: Mapped[str | None] = mapped_column(String(64))
    last_running_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_errors: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    not_found_first_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_found_count: Mapped[int] = mapped_column(Integer, default=0)
    terminate_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_terminate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_terminate_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_terminate_outcome: Mapped[str | None] = mapped_column(String(16))
    credentials_unavailable_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))           # the metering end time
    ended_basis: Mapped[str | None] = mapped_column(String(32))     # provider_reported | first_observed (estimate)
    metered_through: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metering_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    alerts: Mapped[list | None] = mapped_column(JSONB)              # last alerts raised (kind, at)
    validation: Mapped[dict | None] = mapped_column(JSONB)          # live-cycle checks for validation deployments
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # --- 0015_lifecycle ---
    past_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # first seen past deadline
    deadline_retries: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class UsageSlice(Base):
    __tablename__ = "usage_slices"
    __table_args__ = (
        UniqueConstraint("deployment_id", "period_start", name="uq_usage_slice_dep_period"),
        Index("ix_usage_slices_account_period", "account_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(32))
    account_id: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    gpu: Mapped[str] = mapped_column(String(160))
    gpu_count: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    running_seconds: Mapped[int] = mapped_column(Integer, default=0)     # billed at the GPU rate
    stopped_seconds: Mapped[int] = mapped_column(Integer, default=0)     # billed per stopped_billing
    unbilled_seconds: Mapped[int] = mapped_column(Integer, default=0)    # provisioning / unknown-before-running
    billable_seconds: Mapped[int] = mapped_column(Integer, default=0)
    stopped_billing: Mapped[str | None] = mapped_column(String(16))      # full | storage_only | none | n/a
    price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_basis: Mapped[str | None] = mapped_column(String(16))          # execution | quote
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    kind: Mapped[str] = mapped_column(String(16))                        # compute | byo
    usage_record_id: Mapped[int | None] = mapped_column(Integer)
    end_estimated: Mapped[bool] = mapped_column(Boolean, default=False)  # final slice ends at first observation
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderResource(Base):
    """A provider-side resource OpenGrid created for a deployment (ssh_key). status:
    creating (recorded, provider call in flight or crashed) | active | deleting | deleted | delete_failed |
    abandoned (og-* key found at the provider with no usable record, or whose deployment ended; surfaced) |
    not_created (the registration call provably never reached the provider)."""
    __tablename__ = "provider_resources"
    __table_args__ = (
        UniqueConstraint("provider", "credential_ref", "resource_type", "name", name="uq_provider_resource_name"),
        Index("ix_provider_resources_status", "status"),
        Index("ix_provider_resources_dep", "deployment_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32))               # ssh_key
    provider_resource_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128))                        # og-<deployment>
    deployment_id: Mapped[str | None] = mapped_column(String(32))
    credential_ref: Mapped[str] = mapped_column(String(64), default="")  # '' when unknown (never NULL: unique key)
    fingerprint: Mapped[str | None] = mapped_column(String(128))         # SHA256:... of the public key
    recorded_by: Mapped[str | None] = mapped_column(String(32))          # register | reconcile_found
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delete_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    delete_attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_delete_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list | None] = mapped_column(JSONB)                  # [{at, event, ...}] (last 30)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OpsAlertState(Base):
    """One cost-exposure alert condition (kind, subject): never silently resolved."""
    __tablename__ = "ops_alert_state"
    __table_args__ = (
        UniqueConstraint("kind", "subject", name="uq_ops_alert_kind_subject"),
        Index("ix_ops_alert_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48))
    subject: Mapped[str] = mapped_column(String(160))
    deployment_id: Mapped[str | None] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64))
    account_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))                       # open | resolved
    first_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    est_hourly_exposure_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    payload: Mapped[dict | None] = mapped_column(JSONB)                   # the last alert sent
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict | None] = mapped_column(JSONB)                # the evidence it was resolved on
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
