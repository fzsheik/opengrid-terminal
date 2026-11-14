"""Monthly market reports (reports/monthly.py; methodology/monthly-report.md; migration 0039_monthly_reports).

    monthly_reports   one row per calendar month (UTC), 'YYYY-MM': the report generated from stored data only
                      (market_hourly / index_levels / market_events / capacity_changes for live months,
                      archived_prices before live recording), and its separate public-safe summary.

INTERNAL: the report names providers next to their prices and is served only to signed-in readers
(GET /v1/reports/monthly*, data:read; never PUBLIC_PAGES, never the Data API host). `public_summary` is the only
part written to be pasted outside (rounded headline numbers, no provider names).

A row is written once. Regenerating overwrites `report` / `public_summary` / `version` only when explicitly forced,
and keeps `generated_at` (the first generation); `regenerated_at` records the latest forced run. `posted_at` is set
once the compact embed reached the Discord "market" channel.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class MonthlyReport(Base):
    __tablename__ = "monthly_reports"

    month: Mapped[str] = mapped_column(String(7), primary_key=True)          # 'YYYY-MM', UTC calendar month
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # first generation; never changed
    regenerated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last forced regeneration
    version: Mapped[str] = mapped_column(String(16))                         # reports.monthly.VERSION
    report: Mapped[dict] = mapped_column(JSONB)
    public_summary: Mapped[str] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # Discord "market" channel
