"""Routing tables: the audit trail of every routing decision, and what execution actually did.

    route_requests       every /v1/route/preview and /v1/route call, as asked
    routing_decisions    what was considered: every candidate, its factor values and score,
                         every exclusion with its reason, the weights, the market snapshot used
    deployments          one per executed route: provider instance, status, the four prices kept apart
    deployment_events    each status transition, timestamped
    provision_attempts   each provider call made to provision (failover leaves several)
    execution_records    the transaction record: quoted vs actual price, provisioning latency,
                         uptime, interruptions, termination reason; feeds reliability scoring later

Prices are never merged into one column (methodology/data-kinds.md):
    observed_market_price_per_gpu_hour  OpenGrid's stored listing price used for ranking
    list_price_per_gpu_hour             the provider's catalogue price read on the live check
    quoted_price_per_gpu_hour           the price OpenGrid quoted for this route
    actual_price_per_gpu_hour           the execution price the provider reports for the instance

account_id is a plain integer (no cross-domain foreign key); None is the site operator.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
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
    )

    deployment_id: Mapped[str] = mapped_column(String(32), primary_key=True)   # dep_...
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
    # pending | routing | provisioning | running | stopped | failed | terminating | terminated
    status: Mapped[str] = mapped_column(String(16))
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


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"
    __table_args__ = (Index("ix_depev_dep_time", "deployment_id", "at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[str] = mapped_column(String(32))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_status: Mapped[str | None] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict | None] = mapped_column(JSONB)


class ProvisionAttempt(Base):
    __tablename__ = "provision_attempts"
    __table_args__ = (
        Index("ix_pa_dep", "deployment_id"),
        Index("ix_pa_provider_time", "provider", "started_at"),
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
    ok: Mapped[bool] = mapped_column(Boolean)
    error_kind: Mapped[str | None] = mapped_column(String(24))
    error: Mapped[str | None] = mapped_column(Text)


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
