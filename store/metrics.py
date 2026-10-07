"""Metrics, design-partner and product-analytics tables.

    route_outcomes       one row per route request: what was decided (winner, runner-up, candidates,
                         market median, cheapest valid option) and what happened (provisioned?, latency,
                         execution price, interruptions, uptime, savings). Derived: recomputed
                         idempotently by the `route_outcomes` job from the routing tables (routing/quality.py).
    partner_profiles     a design partner's company, contact, workload and what they pay today
    deployment_feedback  the partner's verdict on one deployment (one row per deployment, editable)
    product_events       privacy-preserving product analytics: no IPs, no PII, cookie-less anon ids

account_id is a plain integer (no cross-domain foreign keys), like every other domain.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

PRICE = Numeric(14, 6)
_NOW = dict(server_default=func.now())


class RouteOutcome(Base):
    __tablename__ = "route_outcomes"
    __table_args__ = (
        Index("ix_ro_created", "created_at"),
        Index("ix_ro_account_created", "account_id", "created_at"),
        Index("ix_ro_provider", "winner_provider"),
        Index("ix_ro_open", "final"),
    )

    route_request_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    preview: Mapped[bool] = mapped_column(Boolean)
    gpu: Mapped[str] = mapped_column(String(160))
    strategy: Mapped[str | None] = mapped_column(String(24))
    request_status: Mapped[str | None] = mapped_column(String(32))
    # decision
    winner_provider: Mapped[str | None] = mapped_column(String(64))
    winner_listing_id: Mapped[str | None] = mapped_column(String(256))
    winner_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)        # observed market price
    runner_up_provider: Mapped[str | None] = mapped_column(String(64))
    runner_up_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    candidates_total: Mapped[int] = mapped_column(Integer, default=0)
    candidates: Mapped[list | None] = mapped_column(JSONB)                          # compact top candidates
    market_median_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    market_providers: Mapped[int | None] = mapped_column(Integer)
    cheapest_valid_provider: Mapped[str | None] = mapped_column(String(64))
    cheapest_valid_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    expected_savings_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))        # vs median, at decision
    # execution (null for previews / routes that never launched)
    deployment_id: Mapped[str | None] = mapped_column(String(32))
    deployment_provider: Mapped[str | None] = mapped_column(String(64))
    deployment_purpose: Mapped[str | None] = mapped_column(String(16))
    deployment_status: Mapped[str | None] = mapped_column(String(32))
    launched: Mapped[bool | None] = mapped_column(Boolean)         # a provision call was made
    provisioned: Mapped[bool | None] = mapped_column(Boolean)      # the provider reported it running
    provisioning_latency_ms: Mapped[int | None] = mapped_column(Integer)
    quoted_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    actual_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)        # execution price
    realized_savings_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))        # execution vs median
    quote_error_pct: Mapped[float | None] = mapped_column(Numeric(9, 4))             # (actual - quote) / quote
    interrupted: Mapped[bool | None] = mapped_column(Boolean)
    interruptions: Mapped[int | None] = mapped_column(Integer)
    uptime_seconds: Mapped[int | None] = mapped_column(Integer)
    gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    comparison_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    comparison_reason: Mapped[str | None] = mapped_column(Text)
    final: Mapped[bool] = mapped_column(Boolean, default=False)   # nothing can change any more: skip on recompute
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PartnerProfile(Base):
    __tablename__ = "partner_profiles"

    account_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    technical_contact_name: Mapped[str | None] = mapped_column(String(200))
    technical_contact_email: Mapped[str | None] = mapped_column(String(320))
    preferred_gpus: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    regions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    workload_type: Mapped[str | None] = mapped_column(String(64))
    normal_provider: Mapped[str | None] = mapped_column(String(64))
    normal_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    max_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(PRICE)
    expected_gpu_count: Mapped[int | None] = mapped_column(Integer)
    expected_duration_hours: Mapped[float | None] = mapped_column(Numeric(10, 2))
    latency_requirements: Mapped[str | None] = mapped_column(Text)
    storage_requirements: Mapped[str | None] = mapped_column(Text)
    network_requirements: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="invited")   # invited | onboarding | active
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeploymentFeedback(Base):
    __tablename__ = "deployment_feedback"
    __table_args__ = (Index("ix_feedback_account", "account_id", "created_at"),)

    deployment_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    account_id: Mapped[int | None] = mapped_column(Integer)
    would_have_chosen_provider: Mapped[bool | None] = mapped_column(Boolean)
    price_better: Mapped[bool | None] = mapped_column(Boolean)
    setup_easier: Mapped[bool | None] = mapped_column(Boolean)
    would_route_next: Mapped[bool | None] = mapped_column(Boolean)
    what_broke: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductEvent(Base):
    __tablename__ = "product_events"
    __table_args__ = (
        Index("ix_pe_ts", "ts"),
        Index("ix_pe_event_ts", "event", "ts"),
        Index("ix_pe_anon_ts", "anon_id", "ts"),
        Index("ix_pe_account_ts", "account_id", "ts"),
        Index("ux_pe_dedupe", "dedupe_key", unique=True),   # NULLs (client events) never collide
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    account_id: Mapped[int | None] = mapped_column(Integer)
    anon_id: Mapped[str | None] = mapped_column(String(64))
    event: Mapped[str] = mapped_column(String(32))
    props: Mapped[dict] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(8), default="client")     # client | server
    dedupe_key: Mapped[str | None] = mapped_column(String(128))          # server events: idempotent derivation
