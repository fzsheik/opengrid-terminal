"""The models.

`RawResponse` is what a provider's API said, untouched.
`ComputeListing` is the normalized shape everything else is built on.
`CapacityObservation` is one provider's own word on how much of one SKU it can supply in one region.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RawResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    endpoint: str
    method: str = "GET"
    fetched_at: datetime
    status_code: int | None = None
    duration_ms: int | None = None
    ok: bool = True
    error: str | None = None
    request: dict | list | None = None
    payload: dict | list | None = None


class PriceChange(BaseModel):
    """A price a listing had between two polls and no longer has, from a provider change log (AWS spot)."""

    model_config = ConfigDict(frozen=True)

    provider_changed_at: datetime
    price_per_gpu_hour: Decimal | None
    price_per_instance_hour: Decimal | None


class ComputeListing(BaseModel):
    model_config = ConfigDict(frozen=True)

    # IDENTITY
    provider: str
    sku: str
    listing_id: str

    # HARDWARE
    raw_gpu_name: str
    canonical_gpu_name: str | None
    gpu_count: int

    # LOCATION
    region: str | None
    country: str | None

    # PRICE
    price_per_gpu_hour: Decimal | None
    price_per_instance_hour: Decimal | None
    currency: str = "USD"

    # MARKET CHARACTERISTICS
    market_type: str | None
    provider_tier: str | None
    interruptible: bool | None

    # SUPPLY
    available: bool | None
    capacity: int | None
    capacity_unit: str | None

    # INSTANCE SPECS
    vcpu: int | None
    ram_gb: float | None
    storage_gb: float | None

    # TIME
    observed_at: datetime

    # FX PROVENANCE (fx.py; methodology/fx.md). Set only on a listing the provider priced in another currency and
    # OpenGrid converted to USD; None on every listing priced in USD. The price fields above are always USD.
    original_currency: str | None = None
    original_price_per_gpu_hour: Decimal | None = None
    original_price_per_instance_hour: Decimal | None = None
    fx_rate: Decimal | None = None          # USD per one unit of original_currency
    fx_rate_date: date | None = None        # the reference date of that rate (ECB publication day)
    fx_source: str | None = None            # "ecb"

    # PROVIDER CHANGE TIME (changed_at.py; methodology/freshness.md). When the provider itself says from when this
    # price applies: AWS spot `timestamp`, Nebius spot `effectiveTime` (exact), Azure `effectiveStartDate` (date
    # only), GCP `effectiveTime` (lower bound). Copied onto the history row that records a price change, never onto
    # current state, so excluded from model_dump(). None: the provider gives no time (most do not).
    provider_changed_at: datetime | None = Field(default=None, exclude=True)
    changed_at_basis: Literal["exact", "date_only", "lower_bound"] | None = Field(default=None, exclude=True)
    # Earlier prices from a provider change log that came and went since the previous poll (AWS spot), oldest
    # first, each with its exact time. History only: record_observations() writes one row each.
    superseded: tuple[PriceChange, ...] = Field(default=(), exclude=True)


class CapacityObservation(BaseModel):
    """How much of one SKU a provider says it can supply in one region, right now.

    Providers report capacity in three incompatible shapes, so the shape is kept, not flattened:
        a count     "363 of 395 GPUs free"            -> available_count / total_count
        a tier      "LOW", "scarce"                    -> tier (low | medium | high)
        a boolean   "available": false                 -> in_stock only
    `in_stock` is the one field every shape can fill, so it is the comparable one across providers.
    `raw_value` is the provider's own word, kept for audit. Key: (provider, sku, region).
    """

    model_config = ConfigDict(frozen=True)

    # IDENTITY: same sku as the provider's listings, so capacity joins to price
    provider: str
    sku: str
    region: str | None  # None = the provider reports it provider-wide
    country: str | None = None

    # HARDWARE
    raw_gpu_name: str
    canonical_gpu_name: str | None
    gpu_count: int  # GPUs per instance

    # CAPACITY
    in_stock: bool | None
    tier: Literal["low", "medium", "high"] | None = None
    available_count: int | None = None
    total_count: int | None = None
    count_unit: Literal["gpu", "instance"] | None = None
    raw_value: str

    # PROVENANCE
    source: str  # the endpoint it came from
    observed_at: datetime
