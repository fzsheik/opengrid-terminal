"""Cost-component tables (cost/; methodology/full-cost.md). Raw first, like provider prices and FX.

    cost_snapshots  every fetch of a machine-readable cost source (AWS offer files, Azure Retail Prices, the Oracle
                    price list, Verda volume types), failures included. The body is the provider's JSON, cut to the
                    products cost/ reads (like Oracle's provider fetch keeps only GPU parts). Components are parsed
                    from the newest ok snapshot on read; nothing parsed is stored, so a parser fix re-reads history.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class CostSnapshot(Base):
    __tablename__ = "cost_snapshots"
    __table_args__ = (Index("ix_cost_snapshots_source_time", "source", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64))         # cost.sources.SOURCES key, e.g. "aws_data_transfer"
    provider: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(500))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    ok: Mapped[bool] = mapped_column(Boolean)
    error: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | list | None] = mapped_column(JSONB(none_as_null=True))
