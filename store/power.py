"""Wholesale power-market tables (power/; methodology/power-markets.md). Their own tables, never GPU data.

    power_fetches   every request to a power source, failures and empty answers too: url (keys never stored),
                    status, bytes, sha256 of the exact bytes received and, where the source's per-day size was
                    measured small, the bytes themselves (compressed). Backfill fetches keep the hash only: the URL
                    re-fetches them and the hash verifies the copy (power/registry.py Dataset.keep_raw)
    power_series    the catalogue: one row per series (a hub's day-ahead LMP, a balancing authority's hourly
                    demand). Its visibility comes from the source's terms verdict (power/registry.py), refreshed on
                    every run; "internal" series are admin-only and never in a public read
    power_days      one row per (series, market day): the day's values as real[] (24 hourly, 288 five-minute;
                    23/25 or 276/300 on DST days, NULL where the source gave nothing), plus the day's mean/min/max
                    so day-level reads never unnest. Slot i starts at the market day's local midnight + i x interval.
                    Daily / weekly / monthly series (natural gas: interval_minutes 1440 / 10080 / 43200) hold one
                    value per row, `day` being the date the source labels the period with (week ending, month start)
    power_gpu_region_daily  DERIVED (power/cross.py): per day, provider region and GPU, the local power price beside
                    the GPU price: power cost per GPU-hour (TDP x PUE x price) and the spread. Internal unless every
                    input is public
    power_leadlag   DERIVED (power/cross.py): the correlation of daily GPU price / capacity changes with daily power
                    price changes at lags of -LAG..+LAG days. A correlation, never a cause

Nothing here is read by listings, rollups, indices, fixings, archived prices or any GPU statistic.
"""

from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary
from sqlalchemy import REAL, SmallInteger, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class PowerFetch(Base):
    __tablename__ = "power_fetches"
    __table_args__ = (Index("ix_power_fetches_dataset_period", "dataset", "period_start"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(1000))          # secrets redacted (power/http.py)
    period_start: Mapped[date | None] = mapped_column(Date)  # market days the request covered
    period_end: Mapped[date | None] = mapped_column(Date)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    empty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")  # answered, no data
    error: Mapped[str | None] = mapped_column(Text)
    bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    content_type: Mapped[str | None] = mapped_column(String(80))
    mode: Mapped[str] = mapped_column(String(8))            # "body" (bytes kept) | "hash" (url + sha256 only)
    encoding: Mapped[str | None] = mapped_column(String(8))  # body as stored: "zip" (as received) | "gzip" | None
    body = mapped_column(LargeBinary, nullable=True)   # (an annotation would read the `bytes` column above)
    days_stored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class PowerSeries(Base):
    __tablename__ = "power_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)   # "caiso.da.lmp.th_np15"
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64))
    iso: Mapped[str | None] = mapped_column(String(16))          # market operator / balancing authority group
    commodity: Mapped[str] = mapped_column(String(16), default="electricity", server_default="electricity")
    kind: Mapped[str] = mapped_column(String(32))                # "lmp" | "demand" | "spot_price" | "storage" | ...
    market: Mapped[str] = mapped_column(String(16))              # "da" | "rt" | "actual" | "forecast"
    location: Mapped[str] = mapped_column(String(64))            # the source's own node / zone / BA code
    location_type: Mapped[str] = mapped_column(String(16))       # "hub" | "zone" | "load_area" | "ba"
    unit: Mapped[str] = mapped_column(String(24))                # "usd_per_mwh" | "mwh"
    interval_minutes: Mapped[int] = mapped_column(Integer)      # 5, 15, 60; 1440 daily, 10080 weekly, 43200 monthly
    tz: Mapped[str] = mapped_column(String(40))                  # the market day's time zone
    name: Mapped[str] = mapped_column(String(200))
    visibility: Mapped[str] = mapped_column(String(16))          # "public" | "internal" (the source's terms)
    meta: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerDay(Base):
    __tablename__ = "power_days"
    __table_args__ = (Index("ix_power_days_day", "day"),)

    series_id: Mapped[int] = mapped_column(Integer, ForeignKey("power_series.id", ondelete="CASCADE"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)    # market (operating) day in power_series.tz
    vals: Mapped[list] = mapped_column(ARRAY(REAL))
    n: Mapped[int] = mapped_column(SmallInteger)                 # non-null values
    complete: Mapped[bool] = mapped_column(Boolean)              # every slot of the day has a value
    mean: Mapped[float | None] = mapped_column(REAL)
    min: Mapped[float | None] = mapped_column(REAL)
    max: Mapped[float | None] = mapped_column(REAL)
    fetch_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("power_fetches.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revisions: Mapped[int] = mapped_column(SmallInteger, default=0, server_default="0")


class PowerGpuRegionDaily(Base):
    __tablename__ = "power_gpu_region_daily"
    __table_args__ = (Index("ix_power_gpu_region_daily_gpu_day", "gpu", "day"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    region: Mapped[str] = mapped_column(String(128), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str | None] = mapped_column(String(2))
    iso: Mapped[str] = mapped_column(String(16))
    price_series: Mapped[str] = mapped_column(String(120))       # power_series.key used
    match: Mapped[str] = mapped_column(String(24))               # "zone" (documented place) | "market_area_hub"
    power_price_usd_mwh: Mapped[float] = mapped_column(Float)    # the day's mean
    power_price_max_usd_mwh: Mapped[float | None] = mapped_column(Float)
    tdp_w: Mapped[int] = mapped_column(Integer)
    pue: Mapped[float] = mapped_column(Float)
    power_cost_usd_per_gpu_hour: Mapped[float] = mapped_column(Float)
    gpu_price_median: Mapped[float | None] = mapped_column(Float)   # that day's listings there, USD per GPU-hour
    gpu_price_low: Mapped[float | None] = mapped_column(Float)
    listings: Mapped[int] = mapped_column(Integer)
    spread_usd_per_gpu_hour: Mapped[float | None] = mapped_column(Float)   # median GPU price - power cost
    power_share: Mapped[float | None] = mapped_column(Float)              # power cost / median GPU price
    visibility: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PowerLeadLag(Base):
    __tablename__ = "power_leadlag"
    __table_args__ = (UniqueConstraint("gpu", "iso", "price_series", "metric", "lag_days", "window_end",
                                       name="uq_power_leadlag"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    gpu: Mapped[str] = mapped_column(String(128))
    iso: Mapped[str] = mapped_column(String(16))
    price_series: Mapped[str] = mapped_column(String(120))
    metric: Mapped[str] = mapped_column(String(32))              # "gpu_price_median" | "capacity_available"
    lag_days: Mapped[int] = mapped_column(SmallInteger)          # > 0: the GPU series moves AFTER power
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    n: Mapped[int] = mapped_column(Integer)
    corr: Mapped[float | None] = mapped_column(Float)
    spike_days: Mapped[int] = mapped_column(Integer, default=0)
    visibility: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    method: Mapped[str] = mapped_column(Text)
