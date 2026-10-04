"""The models.

`RawResponse` is what a provider's API said, untouched.
`ComputeListing` is the normalized shape everything else is built on.
"""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


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
