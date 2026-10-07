"""Market-structure tables: daily summaries derived from market_hourly.

    structure_provider_daily  one row per (segment, gpu, provider, day). The per-hour cross-provider
                              comparisons (premium vs the other providers' median, rank, cheapest,
                              priced / available hours) summed per UTC day, so 30/90/365-day provider
                              statistics read a few thousand rows rather than every hour.
                              gpu = '*' is the provider across all its GPUs: comparison sums add up,
                              but hours_priced / hours_tracked count an hour once if ANY GPU qualifies.
    structure_gpu_daily       one row per (segment, gpu, day): dispersion through the day (hourly
                              cross-provider CV, IQR/median, high/low spread), the closing market,
                              provider availability and intraday volatility of the market median.

Both are recomputed from market_hourly by analytics/providers.refresh_daily (an
AFTER_REFRESH hook), so they never disagree with the rollup they come from.
Sums are stored rather than averages so windows combine exactly.
"""

from datetime import date

from sqlalchemy import Date, Float, Index, Integer, Numeric, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class StructureProviderDaily(Base):
    __tablename__ = "structure_provider_daily"
    __table_args__ = (Index("ix_spd_provider_day", "provider", "day"), Index("ix_spd_day", "day"))

    segment: Mapped[str] = mapped_column(String(16), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(160), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    hours_tracked: Mapped[int] = mapped_column(Integer, default=0)    # rollup hours at/after first recorded
    hours_live: Mapped[int] = mapped_column(Integer, default=0)       # >= 1 live listing
    hours_priced: Mapped[int] = mapped_column(Integer, default=0)     # >= 1 priced, not sold-out listing
    hours_available: Mapped[int] = mapped_column(Integer, default=0)  # >= 1 listing explicitly available
    hours_market: Mapped[int] = mapped_column(Integer, default=0)     # tracked AND >= 1 other provider priced
    hours_compared: Mapped[int] = mapped_column(Integer, default=0)   # priced AND >= 1 other provider priced
    premium_sum: Mapped[float] = mapped_column(Float, default=0.0)    # sum of own/median(others) - 1
    hours_cheapest: Mapped[int] = mapped_column(Integer, default=0)   # rank 1 (ties share it)
    hours_top3: Mapped[int] = mapped_column(Integer, default=0)
    rank_counts: Mapped[dict | None] = mapped_column(JSONB)           # {"1": n, ..., "10": n (10 or worse)}
    price_low: Mapped[float | None] = mapped_column(Numeric(14, 6))   # over the provider's hourly lowest price
    price_high: Mapped[float | None] = mapped_column(Numeric(14, 6))
    price_avg: Mapped[float | None] = mapped_column(Numeric(14, 6))
    price_close: Mapped[float | None] = mapped_column(Numeric(14, 6))  # last priced hour of the day


class StructureGpuDaily(Base):
    __tablename__ = "structure_gpu_daily"
    __table_args__ = (Index("ix_sgd_day", "day"),)

    segment: Mapped[str] = mapped_column(String(16), primary_key=True)
    gpu: Mapped[str] = mapped_column(String(160), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)

    hours_market: Mapped[int] = mapped_column(Integer, default=0)      # >= 1 provider priced
    hours_cv: Mapped[int] = mapped_column(Integer, default=0)          # >= 3 providers priced
    cv_sum: Mapped[float] = mapped_column(Float, default=0.0)          # sample stdev / mean, per hour
    iqr_rel_sum: Mapped[float] = mapped_column(Float, default=0.0)     # IQR / median, same hours
    hours_spread: Mapped[int] = mapped_column(Integer, default=0)      # >= 2 providers priced
    spread_sum: Mapped[float] = mapped_column(Float, default=0.0)      # high / low - 1
    providers_max: Mapped[int] = mapped_column(Integer, default=0)
    provider_hours_tracked: Mapped[int] = mapped_column(Integer, default=0)
    provider_hours_priced: Mapped[int] = mapped_column(Integer, default=0)
    close_low: Mapped[float | None] = mapped_column(Numeric(14, 6))
    close_median: Mapped[float | None] = mapped_column(Numeric(14, 6))
    close_high: Mapped[float | None] = mapped_column(Numeric(14, 6))
    close_providers: Mapped[int] = mapped_column(Integer, default=0)
    median_vol: Mapped[float | None] = mapped_column(Float)            # stdev of hourly log changes of the median
    vol_returns: Mapped[int] = mapped_column(Integer, default=0)
