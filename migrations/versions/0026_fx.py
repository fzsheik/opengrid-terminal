"""fx: ECB euro reference rates (raw + parsed) and FX provenance on converted listings (fx.py; methodology/fx.md).
Additive only: two new tables and nullable columns. Prices stay USD; statistics read no new column.

    fx_snapshots                every ECB feed fetch, the XML body verbatim
    fx_rates                    (rate_date, base, quote) -> rate; EUR->USD only (other quotes stay raw)
    compute_listings.+6         original currency / amounts, rate, rate date, source (NULL unless converted)
    listing_observations.+4     original currency / instance amount, rate, rate date (NULL unless converted)

Revision ID: 0026_fx
Revises: 0025_archived_prices
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0026_fx"
down_revision: Union[str, Sequence[str], None] = "0025_archived_prices"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "fx_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("url", sa.String(300), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
    )
    op.create_index("ix_fx_snapshots_source_time", "fx_snapshots", ["source", "fetched_at"])
    op.create_table(
        "fx_rates",
        sa.Column("rate_date", sa.Date(), nullable=False),
        sa.Column("base", sa.String(3), nullable=False),
        sa.Column("quote", sa.String(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 8), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("snapshot_id", sa.BigInteger(), sa.ForeignKey("fx_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("rate_date", "base", "quote"),
    )
    for col in (
        sa.Column("original_currency", sa.String(3), nullable=True),
        sa.Column("original_price_per_gpu_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("original_price_per_instance_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("fx_source", sa.String(16), nullable=True),
    ):
        op.add_column("compute_listings", col)
    for col in (
        sa.Column("original_currency", sa.String(3), nullable=True),
        sa.Column("original_price_per_instance_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("fx_rate", sa.Numeric(18, 8), nullable=True),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
    ):
        op.add_column("listing_observations", col)


def downgrade() -> None:
    for name in ("fx_rate_date", "fx_rate", "original_price_per_instance_hour", "original_currency"):
        op.drop_column("listing_observations", name)
    for name in ("fx_source", "fx_rate_date", "fx_rate", "original_price_per_instance_hour",
                 "original_price_per_gpu_hour", "original_currency"):
        op.drop_column("compute_listings", name)
    op.drop_table("fx_rates")
    op.drop_index("ix_fx_snapshots_source_time", table_name="fx_snapshots")
    op.drop_table("fx_snapshots")
