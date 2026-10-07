"""News tables: raw first, derived after, like the market pipeline.

    news_sources     the registry (news/sources.py) mirrored for ops, plus conditional-GET
                     state (etag / last_modified) and health
    news_fetch_log   one row per fetch attempt: ok, status, error, items, new_items, duration
    news_raw_items   each distinct version of a feed item, untouched (the item's XML or JSON);
                     classification can always be re-run from here
    news_items       one row per article (unique canonical-URL hash), classified, clustered
                     into stories (story_id = id of the story's first item)
    news_entities    (item, type, value) for indexed lookups: gpu | gpu_family | provider |
                     region | topic
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class NewsSource(Base):
    __tablename__ = "news_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(32))
    category: Mapped[str] = mapped_column(String(32))
    trust_tier: Mapped[str] = mapped_column(String(16))
    poll_seconds: Mapped[int] = mapped_column(Integer)
    topics: Mapped[list] = mapped_column(JSONB, default=list)
    entity_hints: Mapped[dict] = mapped_column(JSONB, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(Text)
    # Conditional GET state and health.
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    last_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)


class NewsFetchLog(Base):
    __tablename__ = "news_fetch_log"
    __table_args__ = (Index("ix_nfl_source_time", "source_id", "fetched_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[int | None] = mapped_column(Integer)
    not_modified: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text)
    items: Mapped[int] = mapped_column(Integer, default=0)
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class NewsRawItem(Base):
    __tablename__ = "news_raw_items"
    __table_args__ = (UniqueConstraint("source_id", "raw_hash", name="uq_nri_source_hash"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    raw_hash: Mapped[str] = mapped_column(String(64))
    guid: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    # {"parsed": {...fields as read...}, "raw": {xml | json item}}
    payload: Mapped[dict] = mapped_column(JSONB)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (
        Index("ix_ni_published", "published_at"),
        Index("ix_ni_story", "story_id"),
        Index("ix_ni_source_published", "source_id", "published_at"),
        Index("ix_ni_relevance", "relevance"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    story_id: Mapped[int | None] = mapped_column(BigInteger)
    source_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    canonical_url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str | None] = mapped_column(Text)
    title_key: Mapped[str | None] = mapped_column(Text)   # normalized title, for story clustering
    summary: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # True when the feed gave no readable time (or a future one) and first_seen_at stands in.
    published_at_inferred: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    relevance: Mapped[int] = mapped_column(Integer, default=0)
    relevance_components: Mapped[dict] = mapped_column(JSONB, default=dict)
    topics: Mapped[list] = mapped_column(JSONB, default=list)
    entities: Mapped[dict] = mapped_column(JSONB, default=dict)
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_item_id: Mapped[int | None] = mapped_column(BigInteger)


class NewsEntity(Base):
    __tablename__ = "news_entities"
    __table_args__ = (Index("ix_ne_lookup", "entity_type", "entity_value", "item_id"),)

    item_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("news_items.id", ondelete="CASCADE"), primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    entity_value: Mapped[str] = mapped_column(String(160), primary_key=True)
