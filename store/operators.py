"""Marketplace operators, one row per day (operators.py; methodology/internal/operators.md; migration
0042_operator_daily). Every row INTERNAL: derived from operator keys neither marketplace licenses for republishing.

    operator_daily           per (marketplace, day): machines, machines with an operator key, operators, GPUs, the
                             largest operator's and the top five's share of GPUs, the HHI, and `coverage`, which says
                             what the day's stored data covered (a day read from the 64 cheapest offers per model is
                             not comparable with a whole-book day, and the row says so)
    operator_overlap_daily   per (day, marketplace_a, marketplace_b): machines whose hardware fingerprint matches
                             across the two (strong / build tiers), how many machines of each side matched, the
                             operator pairs behind them, and the median price gap of the strong matches

Written by operators.snapshot_day (the hourly `operators_daily` job writes today's row once the day's window has
closed; `python -m operators backfill` rebuilds past days from the stored raw copies). A day is rebuilt in place.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base

_VIS = "visibility IN ('public', 'internal')"


def _vis():
    return mapped_column(String(8), default="internal", server_default="internal")


class OperatorDaily(Base):
    __tablename__ = "operator_daily"
    __table_args__ = (CheckConstraint(_VIS, name="ck_operator_daily_visibility"),)

    marketplace: Mapped[str] = mapped_column(String(32), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))      # the newest stored copy the row was built from
    machines: Mapped[int] = mapped_column(Integer)
    identified_machines: Mapped[int] = mapped_column(Integer)
    operators: Mapped[int] = mapped_column(Integer)
    gpus: Mapped[int] = mapped_column(Integer)
    top1_gpu_share: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    top5_gpu_share: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))
    hhi: Mapped[int | None] = mapped_column(Integer)
    coverage: Mapped[str] = mapped_column(String(48))                     # operators.COVERAGE_* (what the day's data covered)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()


class OperatorOverlapDaily(Base):
    __tablename__ = "operator_overlap_daily"
    __table_args__ = (CheckConstraint(_VIS, name="ck_operator_overlap_daily_visibility"),)

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    marketplace_a: Mapped[str] = mapped_column(String(32), primary_key=True)
    marketplace_b: Mapped[str] = mapped_column(String(32), primary_key=True)
    comparable_a: Mapped[int] = mapped_column(Integer)                    # machines with a full strong fingerprint
    comparable_b: Mapped[int] = mapped_column(Integer)
    strong_matches: Mapped[int] = mapped_column(Integer)
    build_matches: Mapped[int] = mapped_column(Integer)
    machines_a_matched: Mapped[int] = mapped_column(Integer)
    machines_b_matched: Mapped[int] = mapped_column(Integer)
    operator_pairs: Mapped[int] = mapped_column(Integer)
    median_strong_gap_pct: Mapped[Decimal | None] = mapped_column(Numeric(9, 2))   # b's ask over a's, strong matches
    note: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[str] = _vis()
