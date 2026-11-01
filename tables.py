"""The schema.

    raw_snapshots        every provider response, untouched
    compute_listings     current normalized state, one row per listing
    listing_observations price/supply history, written only when something changes (with when it changed)
    reference_prices     provider price entries that are not listings (kept, not dropped)
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RawPayload(Base):
    """One immutable JSON body per content hash, shared by fetch records."""

    __tablename__ = "raw_payloads"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload: Mapped[dict | list] = mapped_column(JSONB, nullable=False)


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"
    __table_args__ = (
        Index("ix_raw_provider_endpoint_time", "provider", "endpoint", "fetched_at"),
        Index("ix_raw_inline_pending", "id", postgresql_where=text("payload IS NOT NULL AND payload_hash IS NULL")),
        Index("ix_raw_throttled_time", "fetched_at", postgresql_where=text("throttled")),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(64))
    endpoint: Mapped[str] = mapped_column(String(256))
    method: Mapped[str] = mapped_column(String(8), default="GET")
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None]
    duration_ms: Mapped[int | None]
    ok: Mapped[bool] = mapped_column(default=True)
    # Refused for rate (429, or held back while the provider asked us to wait). ok is false (there is no body to
    # read) but it is not a failure: health, ops errors and silence alerts leave it out. NULL on rows before 0028.
    throttled: Mapped[bool | None] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    # The POST body we sent. Salad's availability response carries no class id,
    # so without this a response cannot be matched back to what was asked for.
    request: Mapped[dict | list | None] = mapped_column(JSONB)
    # Inline bodies remain readable during the gradual migration of existing data.
    _inline_payload: Mapped[dict | list | None] = mapped_column("payload", JSONB(none_as_null=True))
    payload_hash: Mapped[str | None] = mapped_column(ForeignKey("raw_payloads.sha256"), index=True)
    _body: Mapped[RawPayload | None] = relationship(lazy="joined")

    @hybrid_property
    def payload(self):
        if self._inline_payload is not None:
            return self._inline_payload
        return self._body.payload if self._body is not None else None

    @payload.setter
    def payload(self, value):
        self._inline_payload = value

    @payload.expression
    def payload(cls):
        shared = select(RawPayload.payload).where(RawPayload.sha256 == cls.payload_hash).scalar_subquery()
        return func.coalesce(cls._inline_payload, shared)


class ComputeListingRow(Base):
    """Current normalized state, one row per listing. Mirrors ComputeListing."""

    __tablename__ = "compute_listings"
    __table_args__ = (
        Index("ix_listing_canonical", "canonical_gpu_name"),
        Index("ix_listing_available", "available"),
        CheckConstraint("visibility IN ('public', 'internal')", name="ck_listing_visibility"),
    )

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    sku: Mapped[str] = mapped_column(String(256))

    raw_gpu_name: Mapped[str] = mapped_column(String(160))
    canonical_gpu_name: Mapped[str | None] = mapped_column(String(160))
    gpu_count: Mapped[int]

    region: Mapped[str | None] = mapped_column(String(64))
    country: Mapped[str | None] = mapped_column(String(8))

    price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_per_instance_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    market_type: Mapped[str | None] = mapped_column(String(24))
    provider_tier: Mapped[str | None] = mapped_column(String(24))
    interruptible: Mapped[bool | None]

    available: Mapped[bool | None]
    capacity: Mapped[int | None]
    capacity_unit: Mapped[str | None] = mapped_column(String(16))

    vcpu: Mapped[int | None]
    ram_gb: Mapped[float | None]
    storage_gb: Mapped[float | None]

    # When this listing was last seen in a fetch. A gap in listing_observations
    # means "nothing changed", and this says how recently we actually looked.
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    # FX provenance (fx.py; methodology/fx.md): only on listings converted to USD from another currency.
    original_currency: Mapped[str | None] = mapped_column(String(3))
    original_price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    original_price_per_instance_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    fx_rate_date: Mapped[date | None]
    fx_source: Mapped[str | None] = mapped_column(String(16))

    # Data tier (0027_visibility; provider_meta.INTERNAL_SOURCES; methodology/data-visibility.md). Public readers
    # filter `visibility = 'public'`; routing / recommendations / job placement read both. An observation belongs
    # to exactly one listing (the foreign key below), so listing_observations inherit the tier by that join.
    visibility: Mapped[str] = mapped_column(String(8), default="public", server_default="public")


class ListingObservation(Base):
    """Price and supply history.

    A row is written only when a tracked value differs from the previous
    observation, so a gap means unchanged. `observed_at` is the provider fetch
    time from the raw snapshot, never the time we got round to normalizing.
    """

    __tablename__ = "listing_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["provider", "listing_id"],
            ["compute_listings.provider", "compute_listings.listing_id"],
            ondelete="CASCADE",
        ),
        Index("ix_obs_listing_time", "provider", "listing_id", "observed_at"),
        Index("ix_obs_time", "observed_at"),
    )

    # `provider` joins the listing_id: a listing_id is only unique per provider.
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    listing_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)

    price_per_gpu_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    price_per_instance_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))

    available: Mapped[bool | None]
    capacity: Mapped[int | None]
    capacity_unit: Mapped[str | None] = mapped_column(String(16))

    # FX provenance of a converted price (see ComputeListingRow): lets history tell a currency move from a
    # list-price move. None for prices observed in USD.
    original_currency: Mapped[str | None] = mapped_column(String(3))
    original_price_per_instance_hour: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    fx_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    fx_rate_date: Mapped[date | None]

    # When the change happened, as far as anyone can say (changed_at.py; methodology/freshness.md).
    #   prev_observed_at     the last fetch that still showed the old value: the change happened in
    #                        (prev_observed_at, observed_at]. NULL on a listing's first row (nothing before it).
    #   provider_changed_at  the provider's own effective time for a price change, when it publishes one.
    #   changed_at_basis     exact | date_only | lower_bound (from provider_changed_at) or poll_window (only the
    #                        bracket above is known). NULL on rows written before 0028 and not yet backfilled.
    prev_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_at_basis: Mapped[str | None] = mapped_column(String(16))


class ReferencePrice(Base):
    """A priced entry from a provider that is not a compute listing.

    Hyperstack's pricebook carries GPU variants that never appear in flavors or
    stocks, plus vCPU, RAM, storage and per-token model pricing. None of it is
    purchasable as a listing, all of it is worth keeping.
    """

    __tablename__ = "reference_prices"
    __table_args__ = (Index("ix_refprice_provider", "provider"),)

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), primary_key=True)

    value: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    original_value: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    discount_applied: Mapped[bool | None] = mapped_column(Boolean)
    currency: Mapped[str] = mapped_column(String(3), default="USD")

    # True when this name is also sold as a listing, so the two can be told apart.
    is_listed: Mapped[bool] = mapped_column(default=False)
    # Our guess at what the name denotes: gpu, compute, storage, network, model_token.
    kind: Mapped[str | None] = mapped_column(String(24))

    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Domain tables live in store/; importing registers them on Base.metadata.
import store  # noqa: E402,F401
