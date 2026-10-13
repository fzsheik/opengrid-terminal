"""Reserved-compute price-lock requests (migration 0019_reservations; methodology/reservations.md).

    reservation_requests   one per customer request ("lock H100 at <= $X/GPU-h for N GPU-hours, starting in
                           window W"): the validated request, the INDICATIVE reference captured when it was
                           made (index level, best on-demand listings, latest settlement fixing; never a quote),
                           the current status, and the operator's offer once one exists (price, validity, terms
                           and the REQUIRED supply basis: the provider capacity commitment that backs it)
    reservation_events     every status transition: from, to, actor, reason, evidence (append-only)

A request's account_id is a plain integer (no cross-domain foreign key); None is the site operator. Nothing here
launches compute, charges, or moves money: an accepted offer is recorded customer intent, and the order form is
signed outside OpenGrid's API.
"""

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class ReservationRequest(Base):
    __tablename__ = "reservation_requests"
    __table_args__ = (
        Index("ix_rsv_account_time", "account_id", "created_at"),
        Index("ix_rsv_status", "status"),
        # An offer without a recorded supply basis cannot exist, whatever code path wrote it.
        CheckConstraint("offer_price_per_gpu_hour IS NULL OR "
                        "(offer_supply_basis IS NOT NULL AND length(btrim(offer_supply_basis)) > 0)",
                        name="ck_rsv_offer_has_supply_basis"),
    )

    request_id: Mapped[str] = mapped_column(String(40), primary_key=True)        # rsv-<hex>
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    principal_kind: Mapped[str | None] = mapped_column(String(16))
    gpu: Mapped[str] = mapped_column(String(128))                               # canonical name
    gpu_count: Mapped[int] = mapped_column(Integer)                             # GPUs used at the same time
    total_gpu_hours: Mapped[float] = mapped_column(Numeric(14, 2))
    start_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    start_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    region_group: Mapped[str | None] = mapped_column(String(32))
    max_price_per_gpu_hour: Mapped[float] = mapped_column(Numeric(12, 6))       # the customer's limit
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24))
    indicative: Mapped[dict | None] = mapped_column(JSONB)                      # reference at request time
    offer_seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")                  # 1, 2, ... per (re-)offer
    offer_price_per_gpu_hour: Mapped[float | None] = mapped_column(Numeric(12, 6))
    offer_valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offer_terms: Mapped[str | None] = mapped_column(Text)
    offer_supply_basis: Mapped[str | None] = mapped_column(Text)               # REQUIRED for any offer
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    offered_by: Mapped[str | None] = mapped_column(String(64))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[str | None] = mapped_column(String(64))
    closed_reason: Mapped[str | None] = mapped_column(Text)                     # decline / withdraw / expiry / cancel
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReservationEvent(Base):
    __tablename__ = "reservation_events"
    __table_args__ = (Index("ix_rsv_events_request", "request_id", "at"), Index("ix_rsv_events_at", "at"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(40))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    from_status: Mapped[str | None] = mapped_column(String(24))
    to_status: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(16))                             # user | admin | system
    actor_id: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[dict | None] = mapped_column(JSONB)
