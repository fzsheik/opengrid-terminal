"""archived prices: price history recovered from public archives (store/archive.py; methodology/archived-history.md).
Additive only: one new table, read by /v1/history/{gpu}/archived and nothing that computes statistics.

Revision ID: 0025_archived_prices
Revises: 0024_probes
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0025_archived_prices"
down_revision: Union[str, Sequence[str], None] = "0025_capacity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "archived_prices",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(24), nullable=False),
        sa.Column("source_url", sa.String(600), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("parser", sa.String(160), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("listing_id", sa.String(256), nullable=False),
        sa.Column("sku", sa.String(256), nullable=True),
        sa.Column("raw_gpu_name", sa.String(256), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=True),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("region", sa.String(64), nullable=True),
        sa.Column("country", sa.String(8), nullable=True),
        sa.Column("price_per_gpu_hour", sa.Numeric(14, 6), nullable=False),
        sa.Column("price_per_instance_hour", sa.Numeric(14, 6), nullable=True),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("market_type", sa.String(16), nullable=True),
        sa.Column("interruptible", sa.Boolean(), nullable=True),
        sa.Column("observed_at", TS, nullable=False),
        sa.Column("valid_to", TS, nullable=True),
        sa.Column("imported_at", TS, nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.UniqueConstraint("source", "source_url", "provider", "listing_id", name="uq_archived_doc_listing"),
    )
    op.create_index("ix_archived_gpu_time", "archived_prices", ["market_type", "gpu", "observed_at"])
    op.create_index("ix_archived_provider_time", "archived_prices", ["provider", "observed_at"])


def downgrade() -> None:
    op.drop_index("ix_archived_provider_time", table_name="archived_prices")
    op.drop_index("ix_archived_gpu_time", table_name="archived_prices")
    op.drop_table("archived_prices")
