"""Freshness log (freshness.py; methodology/freshness.md): one row per provider poll and stream, saying how new the
provider's newest stored data was right after that poll. GET /v1/freshness turns it into ages and percentiles.

    freshness_log  (provider, stream price | capacity, logged_at, data_at). Kept FRESHNESS_LOG_DAYS (8).
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class FreshnessLog(Base):
    __tablename__ = "freshness_log"
    __table_args__ = (Index("ix_freshness_log_key_time", "provider", "stream", "logged_at"),
                      Index("ix_freshness_log_time", "logged_at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))
    stream: Mapped[str] = mapped_column(String(16))
    logged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # when the poll finished
    data_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))     # newest observed_at stored then
