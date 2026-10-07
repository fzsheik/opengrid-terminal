"""news and signals (store/news.py)

Revision ID: 0006_news
Revises: 0005_quality
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0006_news"
down_revision: Union[str, Sequence[str], None] = "0005_quality"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "news_sources",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("trust_tier", sa.String(16), nullable=False),
        sa.Column("poll_seconds", sa.Integer(), nullable=False),
        sa.Column("topics", postgresql.JSONB(), nullable=False),
        sa.Column("entity_hints", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_fetched_at", TS, nullable=True),
        sa.Column("last_ok_at", TS, nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
    )
    op.create_table(
        "news_fetch_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("not_modified", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("items", sa.Integer(), nullable=False),
        sa.Column("new_items", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_nfl_source_time", "news_fetch_log", ["source_id", "fetched_at"])
    op.create_table(
        "news_raw_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("raw_hash", sa.String(64), nullable=False),
        sa.Column("guid", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("source_id", "raw_hash", name="uq_nri_source_hash"),
    )
    op.create_table(
        "news_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("story_id", sa.BigInteger(), nullable=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("title_key", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
        sa.Column("published_at", TS, nullable=False),
        sa.Column("published_at_inferred", sa.Boolean(), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("relevance", sa.Integer(), nullable=False),
        sa.Column("relevance_components", postgresql.JSONB(), nullable=False),
        sa.Column("topics", postgresql.JSONB(), nullable=False),
        sa.Column("entities", postgresql.JSONB(), nullable=False),
        sa.Column("classified_at", TS, nullable=True),
        sa.Column("raw_item_id", sa.BigInteger(), nullable=True),
    )
    op.create_index("ix_ni_published", "news_items", ["published_at"])
    op.create_index("ix_ni_story", "news_items", ["story_id"])
    op.create_index("ix_ni_source_published", "news_items", ["source_id", "published_at"])
    op.create_index("ix_ni_relevance", "news_items", ["relevance"])
    op.create_table(
        "news_entities",
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("news_items.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_value", sa.String(160), nullable=False),
        sa.PrimaryKeyConstraint("item_id", "entity_type", "entity_value"),
    )
    op.create_index("ix_ne_lookup", "news_entities", ["entity_type", "entity_value", "item_id"])


def downgrade() -> None:
    op.drop_table("news_entities")
    op.drop_table("news_items")
    op.drop_table("news_raw_items")
    op.drop_index("ix_nfl_source_time", table_name="news_fetch_log")
    op.drop_table("news_fetch_log")
    op.drop_table("news_sources")
