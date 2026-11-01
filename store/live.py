"""Live market feeds (live/; methodology/live-feeds.md). Raw first, change-only.

    stream_events   one row per CHANGE a push feed reported, with the provider's own timestamp: an item added, an
                    item whose normalised fields changed, an item removed. Frames that change nothing OpenGrid reads
                    (Lium's uptime counter, heartbeats) are counted, never stored. `raw` is the provider's item as
                    sent (the whole executor for Lium), so a reader fix can re-derive from it.

                    basis 'stream'      the change arrived as a live frame: provider_ts is when the provider sent it
                    basis 'resnapshot'  found by diffing a fresh snapshot after a reconnect or a sequence gap: the
                                        change happened after prev_seen_at and no later than provider_ts
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class StreamEvent(Base):
    __tablename__ = "stream_events"
    __table_args__ = (
        Index("ix_stream_events_provider_item_ts", "provider", "item_id", "provider_ts"),
        Index("ix_stream_events_provider_ts", "provider", "provider_ts"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64))
    stream: Mapped[str] = mapped_column(String(64))              # e.g. "lium_ws"
    connection_id: Mapped[str] = mapped_column(String(64))
    seq: Mapped[int | None] = mapped_column(BigInteger)
    provider_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prev_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(16))                # "add" | "change" | "remove"
    basis: Mapped[str] = mapped_column(String(16))               # "stream" | "resnapshot"
    item_id: Mapped[str] = mapped_column(String(256))            # the listing_id the item becomes
    changed_fields: Mapped[list | None] = mapped_column(JSONB)
    kept: Mapped[dict | None] = mapped_column(JSONB)             # the normalised view after the change (None: removed)
    raw: Mapped[dict | None] = mapped_column(JSONB)              # the provider's item as sent (None: removed)
