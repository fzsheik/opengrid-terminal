"""Accounts tables: accounts, API keys, usage, BYO credentials, billing, watchlists and alerts.

    accounts              one OpenGrid account (an org or a person); `is_operator` marks the
                          implicit account the site operator's web UI acts as
    api_keys              `opg_live_...` keys, stored ONLY as HMAC-SHA256(pepper, key)
    api_key_usage         one row per authenticated API request (buffered writes, 90-day retention)
    provider_credentials  BYO provider secrets, Fernet-encrypted at rest

    usage_records         metered compute (GPU-hours, provider cost) per deployment period
    fee_policies          versioned, composable fee components; global default or per account
    charges               priced lines (compute pass-through, fees, credits, subscription, data API)
    credits               prepaid / goodwill balances, consumed by draft invoices
    invoices              DRAFT invoices built from charges; nothing here moves money

    watchlists, watchlist_items, alert_rules, alert_firings

Other domains store `account_id` as a plain integer (no cross-domain foreign keys).
Money is Numeric(14, 6) USD; GPU-hours Numeric(14, 6).
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

_NOW = dict(server_default=func.now())


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (
        # At most one operator account.
        Index("ux_accounts_operator", "is_operator", unique=True, postgresql_where=text("is_operator")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(16), default="active")       # active | suspended
    plan: Mapped[str] = mapped_column(String(32), default="free")
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    is_operator: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_account", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(200))
    prefix: Mapped[str] = mapped_column(String(24))                        # shown in UIs: opg_live_ab12
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True)      # hex HMAC-SHA256; never the key
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_ip: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rate_limit_per_minute: Mapped[int | None] = mapped_column(Integer)


class ApiKeyUsage(Base):
    __tablename__ = "api_key_usage"
    __table_args__ = (
        Index("ix_key_usage_account_ts", "account_id", "ts"),
        Index("ix_key_usage_key_ts", "key_id", "ts"),
        Index("ix_key_usage_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key_id: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(300))
    status: Mapped[int] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class ProviderCredential(Base):
    __tablename__ = "provider_credentials"
    __table_args__ = (Index("ix_provider_credentials_account", "account_id", "provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(64))
    secret_encrypted: Mapped[bytes] = mapped_column(LargeBinary)          # Fernet token
    hint: Mapped[str | None] = mapped_column(String(16))                  # last 4 chars, for display
    label: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---------------------------------------------------------------- billing

class UsageRecord(Base):
    __tablename__ = "usage_records"
    __table_args__ = (
        UniqueConstraint("deployment_id", "period_start", "period_end", name="uq_usage_deployment_period"),
        Index("ix_usage_records_account_period", "account_id", "period_start"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    deployment_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="compute")      # compute | byo
    provider: Mapped[str] = mapped_column(String(64))
    gpu: Mapped[str] = mapped_column(String(160))
    gpu_count: Mapped[int] = mapped_column(Integer)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    gpu_hours: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    provider_cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class FeePolicy(Base):
    __tablename__ = "fee_policies"
    __table_args__ = (Index("ix_fee_policies_scope", "account_id", "effective_from"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    account_id: Mapped[int | None] = mapped_column(Integer)               # null = the global default
    version: Mapped[int] = mapped_column(Integer, default=1)
    components: Mapped[list] = mapped_column(JSONB)                       # see billing/policy.py
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (UniqueConstraint("account_id", "period", name="uq_invoices_account_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    period: Mapped[str] = mapped_column(String(7))                        # YYYY-MM (UTC)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(8), default="draft")       # draft | issued | void
    subtotal_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    credits_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    total_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=0)
    totals: Mapped[dict] = mapped_column(JSONB, default=dict)              # by charge kind
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class Charge(Base):
    """One invoice item. compute = provider cost passed through; fee = OpenGrid's take."""

    __tablename__ = "charges"
    __table_args__ = (
        Index("ix_charges_account_created", "account_id", "created_at"),
        Index("ix_charges_invoice", "invoice_id"),
        Index("ix_charges_usage", "usage_record_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    usage_record_id: Mapped[int | None] = mapped_column(ForeignKey("usage_records.id", ondelete="CASCADE"))
    invoice_id: Mapped[int | None] = mapped_column(ForeignKey("invoices.id", ondelete="SET NULL"))
    credit_id: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))              # compute | fee | credit | subscription | data_api
    description: Mapped[str] = mapped_column(Text)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    policy_id: Mapped[int | None] = mapped_column(Integer)
    component: Mapped[dict | None] = mapped_column(JSONB)     # the fee component that priced it
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class Credit(Base):
    __tablename__ = "credits"
    __table_args__ = (Index("ix_credits_account", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    remaining_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    reason: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


# ---------------------------------------------------------------- watchlists and alerts

class Watchlist(Base):
    __tablename__ = "watchlists"
    __table_args__ = (Index("ix_watchlists_account", "account_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (Index("ix_watchlist_items_list", "watchlist_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watchlist_id: Mapped[int] = mapped_column(ForeignKey("watchlists.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(16))             # gpu | provider | gpu_provider | region | index
    gpu: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(64))
    region_group: Mapped[str | None] = mapped_column(String(32))
    index_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class AlertRule(Base):
    __tablename__ = "alert_rules"
    __table_args__ = (Index("ix_alert_rules_account", "account_id"), Index("ix_alert_rules_status", "status"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(Integer)
    name: Mapped[str | None] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(48))             # the metric name, e.g. market_low
    params: Mapped[dict] = mapped_column(JSONB)
    channels: Mapped[list] = mapped_column(JSONB, default=list)   # [{"type":"in_app"}, {"type":"webhook","url":...}]
    webhook_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(16), default="active")    # active | paused
    cooldown_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_state: Mapped[str | None] = mapped_column(String(8))           # true | false | unknown
    last_known_state: Mapped[str | None] = mapped_column(String(8))     # true | false (unknown never overwrites)
    last_value: Mapped[float | None]
    last_detail: Mapped[str | None] = mapped_column(Text)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), **_NOW)


class AlertFiring(Base):
    __tablename__ = "alert_firings"
    __table_args__ = (
        Index("ix_alert_firings_account_time", "account_id", "fired_at"),
        Index("ix_alert_firings_rule_time", "rule_id", "fired_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(Integer)
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value: Mapped[float | None]
    message: Mapped[str] = mapped_column(Text)
    delivered_via: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    delivery_status: Mapped[dict] = mapped_column(JSONB, default=dict)
