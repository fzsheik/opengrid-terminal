"""Capacity tables (capacity.py; methodology/capacity.md). Same pattern as listings: current state, plus history
written only when something changes.

    capacity_current  one row per (provider, sku, region): the newest observation, when it was first seen, and when
                      its state last changed
    capacity_changes  one row each time a (provider, sku, region) changes state (in stock, tier or counts). A gap
                      means nothing moved. Rebuildable from raw_snapshots by re-running the normalizers.

`region_key` is the region, or "" when the provider reports a SKU provider-wide (a primary key cannot be NULL).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class CapacityCurrent(Base):
    __tablename__ = "capacity_current"

    provider: Mapped[str] = mapped_column(String(64), primary_key=True)
    sku: Mapped[str] = mapped_column(String(200), primary_key=True)
    region_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    region: Mapped[str | None] = mapped_column(String(96))
    country: Mapped[str | None] = mapped_column(String(8))
    raw_gpu_name: Mapped[str] = mapped_column(String(200))
    canonical_gpu_name: Mapped[str | None] = mapped_column(String(200), index=True)
    gpu_count: Mapped[int] = mapped_column(Integer)
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    tier: Mapped[str | None] = mapped_column(String(8))
    available_count: Mapped[int | None] = mapped_column(Integer)
    total_count: Mapped[int | None] = mapped_column(Integer)
    count_unit: Mapped[str | None] = mapped_column(String(16))
    raw_value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # data tier (0027_visibility; methodology/data-visibility.md): public readers filter visibility = 'public'
    visibility: Mapped[str] = mapped_column(String(8), default="public", server_default="public")


class CapacityChange(Base):
    __tablename__ = "capacity_changes"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))
    sku: Mapped[str] = mapped_column(String(200))
    region_key: Mapped[str] = mapped_column(String(96))
    canonical_gpu_name: Mapped[str | None] = mapped_column(String(200))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    in_stock: Mapped[bool | None] = mapped_column(Boolean)
    tier: Mapped[str | None] = mapped_column(String(8))
    available_count: Mapped[int | None] = mapped_column(Integer)
    total_count: Mapped[int | None] = mapped_column(Integer)
    raw_value: Mapped[str] = mapped_column(Text)
    visibility: Mapped[str] = mapped_column(String(8), default="public", server_default="public")

    __table_args__ = (Index("ix_capacity_changes_key_time", "provider", "sku", "region_key", "observed_at"),
                      Index("ix_capacity_changes_gpu_time", "canonical_gpu_name", "observed_at"))
