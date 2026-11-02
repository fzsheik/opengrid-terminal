"""news history + major shifts (news/history.py, analytics/shifts.py; methodology/news-history.md, shifts.md).
Additive only: nullable / defaulted columns on the news tables and two new tables.

    news_raw_items.+3    origin (live | wayback_feed | feed_archive | wayback_index | gdelt | edgar), archive_url,
                         captured_at: where a recovered item was read and when the archive captured it
    news_items.+2        backfilled, provenance
    market_shifts        major shifts (observed and archived), idempotent on shift_key
    market_shift_items   ranked related items stored per shift (never causal)

Revision ID: 0029_news_history
Revises: 0026_fx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0029_news_history"
down_revision: Union[str, Sequence[str], None] = "0033_equities"  # re-chained at the 2026-11-02b merge
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("news_raw_items", sa.Column("origin", sa.String(16), nullable=False, server_default="live"))
    op.add_column("news_raw_items", sa.Column("archive_url", sa.Text(), nullable=True))
    op.add_column("news_raw_items", sa.Column("captured_at", TS, nullable=True))
    op.add_column("news_items", sa.Column("backfilled", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.add_column("news_items", sa.Column("provenance", JSONB(), nullable=True))

    op.create_table(
        "market_shifts",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("shift_key", sa.String(300), nullable=False, unique=True),
        sa.Column("data_kind", sa.String(16), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("segment", sa.String(16), nullable=True),
        sa.Column("gpu", sa.String(160), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("index_id", sa.String(96), nullable=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("window_start", TS, nullable=False),
        sa.Column("window_end", TS, nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("magnitude", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(10), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("detail", JSONB(), nullable=True),
        sa.Column("definition_version", sa.String(16), nullable=False),
        sa.Column("detected_at", TS, nullable=False),
        sa.Column("items_version", sa.String(16), nullable=True),
        sa.Column("items_computed_at", TS, nullable=True),
    )
    op.create_index("ix_ms_at", "market_shifts", ["at"])
    op.create_index("ix_ms_gpu_at", "market_shifts", ["gpu", "at"])
    op.create_index("ix_ms_provider_at", "market_shifts", ["provider", "at"])
    op.create_table(
        "market_shift_items",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("shift_id", sa.BigInteger(), sa.ForeignKey("market_shifts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(24), nullable=False),
        sa.Column("ref_id", sa.BigInteger(), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("hours_from_shift", sa.Float(), nullable=False),
        sa.Column("position", sa.String(8), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_components", JSONB(), nullable=False),
        sa.Column("snapshot", JSONB(), nullable=False),
        sa.Column("match", sa.Text(), nullable=True),
        sa.UniqueConstraint("shift_id", "item_type", "ref_id", name="uq_msi_shift_item"),
    )
    op.create_index("ix_market_shift_items_shift_id", "market_shift_items", ["shift_id"])


def downgrade() -> None:
    op.drop_index("ix_market_shift_items_shift_id", table_name="market_shift_items")
    op.drop_table("market_shift_items")
    op.drop_index("ix_ms_provider_at", table_name="market_shifts")
    op.drop_index("ix_ms_gpu_at", table_name="market_shifts")
    op.drop_index("ix_ms_at", table_name="market_shifts")
    op.drop_table("market_shifts")
    op.drop_column("news_items", "provenance")
    op.drop_column("news_items", "backfilled")
    op.drop_column("news_raw_items", "captured_at")
    op.drop_column("news_raw_items", "archive_url")
    op.drop_column("news_raw_items", "origin")
