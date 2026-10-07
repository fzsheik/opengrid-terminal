"""Analytics tables: the hourly market rollup everything historical reads from.

    market_hourly   one row per (segment, gpu, provider, region, hour): the state of that
                    provider's eligible listings sampled at the top of the hour
    index_levels    one row per (index, hour): the OpenGrid index level computed from
                    market_hourly (see analytics/indices.py, methodology/indices.md)

Sampling follows market.py exactly (a listing's state at time t is its last
observation at or before t; it counts only until last seen + stale_after), so a
chart drawn from rollups agrees with one drawn from raw observations.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Double, Index, Integer, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class MarketHourly(Base):
    __tablename__ = "market_hourly"
    __table_args__ = (
        Index("ix_mh_gpu_hour", "segment", "gpu", "hour"),
        Index("ix_mh_provider_hour", "provider", "hour"),
        Index("ix_mh_hour", "hour"),
    )

    # on_demand: market.py's eligibility rules. spot: spot or interruptible, same exclusions otherwise.
    segment: Mapped[str] = mapped_column(String(16), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The provider's own region string; '' when the listing has none. Grouping is a read-time concern.
    region: Mapped[str] = mapped_column(String(64), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    country: Mapped[str | None] = mapped_column(String(8))

    # Over listings live AND not sold out at the sample time (price > 0). Null when none.
    min_price: Mapped[float | None] = mapped_column(Numeric(14, 6))
    max_price: Mapped[float | None] = mapped_column(Numeric(14, 6))
    avg_price: Mapped[float | None] = mapped_column(Numeric(14, 6))
    # Listing counts at the sample time. live = recorded and not yet gone.
    live_listings: Mapped[int] = mapped_column(Integer, default=0)
    priced_listings: Mapped[int] = mapped_column(Integer, default=0)      # live, priced, not sold out
    available_listings: Mapped[int] = mapped_column(Integer, default=0)   # available is True
    unknown_listings: Mapped[int] = mapped_column(Integer, default=0)     # available is None
    sold_out_listings: Mapped[int] = mapped_column(Integer, default=0)    # available is False
    # Sum of capacity where the provider reports it in GPUs (capacity_unit = 'gpu'); null otherwise.
    capacity_gpus: Mapped[int | None] = mapped_column(Integer)


class IndexLevel(Base):
    """One index, one hour. Rows exist only for hours where the index had at least one
    candidate constituent; an unpublished row (published=False) says why in `reason`.

    Levels are chain-linked (see methodology/indices.md), so `segment_no` changes only
    when the chain had to be rebased; levels in different segments are not comparable.
    """

    __tablename__ = "index_levels"
    __table_args__ = (
        Index("ix_il_hour", "hour"),
        # high / low lookups within the current chain segment
        Index("ix_il_extremes", "index_id", "segment_no", "level", postgresql_where=text("published")),
    )

    index_id: Mapped[str] = mapped_column(String(96), primary_key=True)
    hour: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    level: Mapped[float | None] = mapped_column(Double)        # the index (chain-linked); null unless published
    raw_level: Mapped[float | None] = mapped_column(Double)    # the plain cross-sectional statistic this hour
    constituents: Mapped[int] = mapped_column(Integer, default=0)       # included after exclusions
    link_constituents: Mapped[int | None] = mapped_column(Integer)      # common with the previous published hour
    method: Mapped[str | None] = mapped_column(String(32))
    segment_no: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    detail: Mapped[list | None] = mapped_column(JSONB)         # each candidate constituent and its fate
    methodology_version: Mapped[str] = mapped_column(String(16))
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
