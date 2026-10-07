"""Routing tables: the audit trail of every routing decision, and what execution actually did.

    route_requests       every /v1/route/preview and /v1/route call, as asked
    routing_decisions    what was considered: every candidate, its factor values and score,
                         every exclusion with its reason, the weights, the market snapshot used
    deployments          one per executed route: provider instance, status, the four prices kept apart
    deployment_events    each status transition, timestamped
    provision_attempts   each provider call made to provision (failover leaves several)
    execution_records    the transaction record: quoted vs actual price, provisioning latency,
                         uptime, interruptions, termination reason; feeds reliability scoring later

Execution control (migration 0010_execution, methodology/execution-safety.md):
    execution_controls        key/value runtime switches (the global execution mode)
    execution_control_log     append-only log of every control/admin change, with actor and reason
    provider_execution_flags  per provider: adapter_status simulated|validated, supervised/live enable, kill
    quotes                    priced, expiring offers a launch must reference (q_...)
    idempotency_keys          Idempotency-Key replay store; UNIQUE(principal, scope, key)
    account_limits            per-account cost guards (settings hold the defaults)

Prices are never merged into one column (methodology/data-kinds.md):
    observed_market_price_per_gpu_hour  OpenGrid's stored listing price used for ranking
    list_price_per_gpu_hour             the provider's catalogue price read on the live check
    quoted_price_per_gpu_hour           the price OpenGrid quoted for this route
    actual_price_per_gpu_hour           the execution price the provider reports for the instance

account_id is a plain integer (no cross-domain foreign key); None is the site operator.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

PRICE = Numeric(14, 6)


class RouteRequest(Base):
    __tablename__ = "route_requests"
    __table_args__ = (
        Index("ix_rr_account_time", "account_id", "created_at"),
        Index("ix_rr_time", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)        # rr_...
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    principal_kind: Mapped[str] = mapped_column(String(16))
    preview: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[str] = mapped_column(String(24))
    gpu: Mapped[str] = mapped_column(String(160))
    request: Mapped[dict] = mapped_column(JSONB)
    # previewed | not_provisioned | provisioned | failed | no_candidates
    status: Mapped[str] = mapped_column(String(24))
    result: Mapped[dict | None] = mapped_column(JSONB)                   # summary of what was returned
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RoutingDecision(Base):
    __tablename__ = "routing_decisions"
    __table_args__ = (Index("ix_rd_request", "route_request_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    route_request_id: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(24))
    weights: Mapped[dict] = mapped_column(JSONB)
    candidates: Mapped[list] = mapped_column(JSONB)
    multi_instance: Mapped[list | None] = mapped_column(JSONB)
    exclusions: Mapped[list] = mapped_column(JSONB)
    selected_provider: Mapped[str | None] = mapped_column(String(64))
    selected_listing_id: Mapped[str | None] = mapped_column(String(256))
    selected_observed_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    market_snapshot: Mapped[dict] = mapped_column(JSONB)
    methodology_version: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Deployment(Base):
    __tablename__ = "deployments"
    __table_args__ = (
        Index("ix_dep_account_time", "account_id", "created_at"),
        Index("ix_dep_status", "status"),
        Index("ix_dep_request", "route_request_id"),
        Index("ux_dep_launch_token", "launch_token", unique=True),
    )

    deployment_id: Mapped[str] = mapped_column(String(32), primary_key=True)   # dep-<hex> (legacy: dep_<hex>)
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    route_request_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64))       # null until a provider accepted it
    listing_id: Mapped[str | None] = mapped_column(String(256))
    provider_instance_id: Mapped[str | None] = mapped_column(String(128))
    gpu: Mapped[str] = mapped_column(String(160))
    gpu_count: Mapped[int] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(String(64))
    observed_market_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    list_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    quoted_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    quote_basis: Mapped[str | None] = mapped_column(String(32))
    actual_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)   # null until known
    # the state machine in routing/deployments.py (ALLOWED_TRANSITIONS)
    status: Mapped[str] = mapped_column(String(32))
    provider_status: Mapped[str | None] = mapped_column(String(64))            # the provider's own word
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    running_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    interruptions: Mapped[int] = mapped_column(Integer, default=0)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    termination_reason: Mapped[str | None] = mapped_column(String(64))
    credential_source: Mapped[str | None] = mapped_column(String(16))         # opengrid | byo
    launch: Mapped[dict | None] = mapped_column(JSONB)                        # the canonical launch spec
    provider_metadata: Mapped[dict | None] = mapped_column(JSONB)             # debugging only, never public
    # --- 0010_execution -------------------------------------------------------------------
    purpose: Mapped[str] = mapped_column(String(16), default="customer")     # customer | validation
    quote_id: Mapped[str | None] = mapped_column(String(40))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_mode: Mapped[str | None] = mapped_column(String(16))            # SUPERVISED | LIVE
    max_runtime_minutes: Mapped[int | None] = mapped_column(Integer)
    terminate_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    launch_token: Mapped[str | None] = mapped_column(String(64))             # set once: the one provision call
    client_name: Mapped[str | None] = mapped_column(String(80))              # og-<deployment_id>, sent to the provider
    credential_ref: Mapped[str | None] = mapped_column(String(64))           # byo:<id> | platform:<provider>
    credential_account_id: Mapped[int | None] = mapped_column(Integer)       # whose BYO row (operator acct too)
    limit_violations: Mapped[list | None] = mapped_column(JSONB)
    override_limits: Mapped[bool] = mapped_column(Boolean, default=False)
    override_reason: Mapped[str | None] = mapped_column(Text)
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terminate_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_reported_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    reconciled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reconciliation: Mapped[dict | None] = mapped_column(JSONB)


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"
    __table_args__ = (Index("ix_depev_dep_time", "deployment_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict | None] = mapped_column(JSONB)
    actor: Mapped[str | None] = mapped_column(String(16))         # user | admin | system | reconciler
    actor_id: Mapped[str | None] = mapped_column(String(64))      # key:<id> | operator | job name
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)


class ProvisionAttempt(Base):
    __tablename__ = "provision_attempts"
    __table_args__ = (
        Index("ix_pa_dep", "deployment_id"),
        Index("ix_pa_provider_time", "provider", "started_at"),
        Index("ux_pa_launch_token", "launch_token", unique=True),
        Index("ix_pa_outcome", "outcome"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(32))
    route_request_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(64))
    listing_id: Mapped[str | None] = mapped_column(String(256))
    rank: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool | None] = mapped_column(Boolean)        # null while the call is in flight
    error_kind: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)
    # --- 0010_execution: written BEFORE the provider call (write-ahead), updated after -----------
    outcome: Mapped[str | None] = mapped_column(String(16))      # provisioning | accepted | rejected | unknown
    launch_token: Mapped[str | None] = mapped_column(String(64))
    client_name: Mapped[str | None] = mapped_column(String(80))
    credential_ref: Mapped[str | None] = mapped_column(String(64))
    quote_id: Mapped[str | None] = mapped_column(String(40))
    instance_id: Mapped[str | None] = mapped_column(String(128))
    status_code: Mapped[int | None] = mapped_column(Integer)
    provider_request_id: Mapped[str | None] = mapped_column(String(128))
    request_summary: Mapped[dict | None] = mapped_column(JSONB)


class ExecutionRecord(Base):
    """The transaction record of one deployment. Kind: transaction."""

    __tablename__ = "execution_records"
    __table_args__ = (
        Index("ix_exec_provider_time", "provider", "created_at"),
        Index("ix_exec_account", "account_id"),
    )

    deployment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    route_request_id: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str | None] = mapped_column(String(64))
    gpu: Mapped[str] = mapped_column(String(160))
    gpu_count: Mapped[int] = mapped_column(Integer)
    observed_market_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    quoted_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    actual_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    provision_ok: Mapped[bool] = mapped_column(Boolean)
    provision_latency_ms: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    interruptions: Mapped[int] = mapped_column(Integer, default=0)
    termination_reason: Mapped[str | None] = mapped_column(String(64))
    workload_completed: Mapped[bool | None] = mapped_column(Boolean)          # only when the caller reports it
    cost_basis: Mapped[str | None] = mapped_column(String(16))                # actual | quote
    provider_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    usage_record_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------
# 0010_execution: control plane, quotes, idempotency, limits
# --------------------------------------------------------------------------

class ExecutionControl(Base):
    """Runtime switches, one row per key ('mode'). No row: the documented default (PREVIEW_ONLY)."""

    __tablename__ = "execution_controls"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ExecutionControlLog(Base):
    """Append-only: every mode/flag/kill change and every admin execution action."""

    __tablename__ = "execution_control_log"
    __table_args__ = (Index("ix_ecl_time", "at"), Index("ix_ecl_target", "target"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[str] = mapped_column(String(48))
    target: Mapped[str | None] = mapped_column(String(128))
    before: Mapped[dict | None] = mapped_column(JSONB)
    after: Mapped[dict | None] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(String(64))


class ProviderExecutionFlags(Base):
    __tablename__ = "provider_execution_flags"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    adapter_status: Mapped[str] = mapped_column(String(16), default="simulated")   # simulated | validated
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validation_deployment_id: Mapped[str | None] = mapped_column(String(32))
    validation_evidence: Mapped[dict | None] = mapped_column(JSONB)
    supervised_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    live_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    killed: Mapped[bool] = mapped_column(Boolean, default=False)
    kill_reason: Mapped[str | None] = mapped_column(Text)
    killed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    killed_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(64))


class QuoteRow(Base):
    """A priced, expiring offer. A launch must reference an active, unexpired, revalidated quote.

    Price concepts kept apart: observed_price_per_gpu_hour (market observation the ranking used),
    quote_price_per_gpu_hour (the price OpenGrid quotes, after the live check when there was one).
    """

    __tablename__ = "quotes"
    __table_args__ = (Index("ix_quotes_rr", "route_request_id"),
                      Index("ix_quotes_account_time", "account_id", "created_at"))

    id: Mapped[str] = mapped_column(String(40), primary_key=True)              # q_<hex>
    route_request_id: Mapped[str | None] = mapped_column(String(32))
    account_id: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(64))
    listing_id: Mapped[str] = mapped_column(String(256))
    offer: Mapped[dict] = mapped_column(JSONB)                                 # Offer snapshot
    availability: Mapped[dict | None] = mapped_column(JSONB)                   # live-check snapshot
    gpu: Mapped[str] = mapped_column(String(160))
    gpu_count: Mapped[int] = mapped_column(Integer)
    region: Mapped[str | None] = mapped_column(String(64))
    region_group: Mapped[str | None] = mapped_column(String(32))
    observed_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    quote_price_per_gpu_hour: Mapped[Decimal] = mapped_column(PRICE)
    est_hourly_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    est_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    fees: Mapped[dict | None] = mapped_column(JSONB)
    taxes: Mapped[dict | None] = mapped_column(JSONB)
    billing_unit: Mapped[str | None] = mapped_column(String(64))
    minimum_commitment: Mapped[str | None] = mapped_column(String(128))
    price_source: Mapped[str] = mapped_column(String(16))                      # observed | live_check
    purpose: Mapped[str] = mapped_column(String(16), default="customer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))                            # active|consumed|expired|superseded
    consumed_by_deployment_id: Mapped[str | None] = mapped_column(String(32))
    superseded_by: Mapped[str | None] = mapped_column(String(40))


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("principal", "scope", "key", name="ux_idem_principal_scope_key"),
                      Index("ix_idem_expires", "expires_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    principal: Mapped[str] = mapped_column(String(64))        # acct:<id> | operator
    scope: Mapped[str] = mapped_column(String(128))           # route | approve:<rr> | terminate:<dep> | ...
    key: Mapped[str] = mapped_column(String(255))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))           # in_progress | completed | failed
    response_code: Mapped[int | None] = mapped_column(Integer)
    response: Mapped[dict | None] = mapped_column(JSONB)
    resource_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AccountLimits(Base):
    """Per-account cost guards. NULL column: the settings default applies (routing/guards.py)."""

    __tablename__ = "account_limits"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    max_hourly_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    max_total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    max_gpus: Mapped[int | None] = mapped_column(Integer)
    max_active_deployments: Mapped[int | None] = mapped_column(Integer)
    provider_allowlist: Mapped[list | None] = mapped_column(ARRAY(String(64)))
    region_allowlist: Mapped[list | None] = mapped_column(ARRAY(String(64)))
    monthly_spend_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str | None] = mapped_column(String(64))
