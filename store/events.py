"""Market events tables.

    market_events          structured, deduplicated market events (price moves, sell-outs,
                           records, feed outages...). One row per event; `dedupe_key` makes
                           detection idempotent: re-running the detector over the same hours
                           writes nothing new.
    market_gpu_hourly      one row per (segment, gpu, hour): the cross-provider market state the
                           detector derived from market_hourly, plus coverage-matched changes.
                           Kept so 30-day baselines (records, volatility, z-scores, availability
                           norms) read a few hundred rows instead of replaying provider history.
    event_detector_state   watermarks: the last hour / fetch each detector has processed.

Also declares an index on raw_snapshots(fetched_at): feed-outage detection reads
recent fetches across providers, which the original (provider, endpoint, time)
index cannot serve without scanning every provider's whole history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base, RawSnapshot

Index("ix_raw_fetched_at", RawSnapshot.__table__.c.fetched_at)


class MarketEvent(Base):
    __tablename__ = "market_events"
    __table_args__ = (
        Index("ix_me_occurred", "occurred_at"),
        Index("ix_me_gpu_occurred", "gpu", "occurred_at"),
        Index("ix_me_provider_occurred", "provider", "occurred_at"),
        Index("ix_me_type_occurred", "type", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # when it happened (hour sampled)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # when the detector wrote it
    type: Mapped[str] = mapped_column(String(40))
    segment: Mapped[str | None] = mapped_column(String(16))
    gpu: Mapped[str | None] = mapped_column(String(160))
    provider: Mapped[str | None] = mapped_column(String(64))
    region_group: Mapped[str | None] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(10))                        # info | notable | major
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[dict | None] = mapped_column(JSONB)                       # the numbers behind it
    value_before: Mapped[float | None] = mapped_column(Numeric(14, 6))
    value_after: Mapped[float | None] = mapped_column(Numeric(14, 6))
    pct: Mapped[float | None] = mapped_column(Float)                         # fraction: -0.124 = -12.4%
    dedupe_key: Mapped[str] = mapped_column(String(300), unique=True)


class MarketGpuHourly(Base):
    __tablename__ = "market_gpu_hourly"
    __table_args__ = (Index("ix_mgh_hour", "hour"),)

    segment: Mapped[str] = mapped_column(String(16), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(160), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    # Over every provider recorded at this hour (as-recorded, no coverage filter).
    providers_live: Mapped[int] = mapped_column(Integer, default=0)
    providers_priced: Mapped[int] = mapped_column(Integer, default=0)
    providers_available: Mapped[int] = mapped_column(Integer, default=0)
    live_listings: Mapped[int] = mapped_column(Integer, default=0)
    priced_listings: Mapped[int] = mapped_column(Integer, default=0)
    available_listings: Mapped[int] = mapped_column(Integer, default=0)
    sold_out_listings: Mapped[int] = mapped_column(Integer, default=0)
    lowest: Mapped[float | None] = mapped_column(Numeric(14, 6))
    median: Mapped[float | None] = mapped_column(Numeric(14, 6))
    highest: Mapped[float | None] = mapped_column(Numeric(14, 6))
    # Coverage-matched changes: only providers priced at BOTH times, so a provider we began
    # recording in between cannot look like a market move. Fractions; null when not computable.
    chg1h_median: Mapped[float | None] = mapped_column(Float)
    chg24_median: Mapped[float | None] = mapped_column(Float)
    chg24_lowest: Mapped[float | None] = mapped_column(Float)


class EventDetectorState(Base):
    __tablename__ = "event_detector_state"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    info: Mapped[dict | None] = mapped_column(JSONB)
