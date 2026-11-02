"""equities (INTERNAL research only): a visibility marker on context_series and an equity_bars table for hourly
Alpaca bars (context/equities.py; methodology/internal/equities.md). Additive only.

    context_series.visibility   "public" (default; EIA, EDGAR) | "internal" (Alpaca equities): never served publicly
    equity_bars                 (symbol, timeframe, adjustment, t) -> OHLC, volume, vwap, trades; internal

Revision ID: 0033_equities
Revises: 0032_context_series
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0033_equities"
down_revision: Union[str, Sequence[str], None] = "0032_context_series"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
PX = sa.Numeric(18, 6)


def upgrade() -> None:
    op.add_column("context_series", sa.Column("visibility", sa.String(16), nullable=False, server_default="public"))
    op.create_table(
        "equity_bars",
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("adjustment", sa.String(8), nullable=False),
        sa.Column("t", TS, nullable=False),
        sa.Column("open", PX, nullable=False),
        sa.Column("high", PX, nullable=False),
        sa.Column("low", PX, nullable=False),
        sa.Column("close", PX, nullable=False),
        sa.Column("volume", sa.Numeric(24, 6), nullable=False),
        sa.Column("vwap", PX, nullable=True),
        sa.Column("trades", sa.BigInteger(), nullable=True),
        sa.Column("feed", sa.String(8), nullable=False),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("revisions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("context_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.PrimaryKeyConstraint("symbol", "timeframe", "adjustment", "t"),
    )
    op.create_index("ix_equity_bars_t", "equity_bars", ["timeframe", "t"])


def downgrade() -> None:
    op.drop_index("ix_equity_bars_t", table_name="equity_bars")
    op.drop_table("equity_bars")
    op.drop_column("context_series", "visibility")
