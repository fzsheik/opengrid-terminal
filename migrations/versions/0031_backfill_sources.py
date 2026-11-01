"""backfill sources: provenance per archived point, and raw third-party catalogue snapshots
(store/archive.py; methodology/archived-history.md). Additive only: nullable columns and one new table.
Nothing that computes statistics reads any of it.

    archived_prices.basis         what one point means: capture | price_list_version | effective_since |
                                  dataset_snapshot | monthly_median | catalog_commit (NULL on rows imported before
                                  this revision = capture or price_list_version, by source)
    archived_prices.licence       the licence the point was published under (e.g. MIT, CC-BY-4.0, or the
                                  publisher's own terms for official price lists)
    archived_prices.attribution   who to credit, verbatim as the licence asks
    archived_prices.corroboration other sources that said the same thing for the same provider / listing / date:
                                  a JSON list of {source, source_url, observed_at, price_per_gpu_hour, licence};
                                  the corroborating source is recorded here instead of as a second point
    catalog_snapshots             one downloaded third-party catalogue file (e.g. dstack's gpuhunt catalog.zip),
                                  bytes verbatim, so it survives the publisher's retention

Revision ID: 0031_backfill_sources
Revises: 0030_cost_components
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0031_backfill_sources"
down_revision: Union[str, Sequence[str], None] = "0030_cost_components"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("archived_prices", sa.Column("basis", sa.String(24), nullable=True))
    op.add_column("archived_prices", sa.Column("licence", sa.String(64), nullable=True))
    op.add_column("archived_prices", sa.Column("attribution", sa.Text(), nullable=True))
    op.add_column("archived_prices", sa.Column("corroboration", JSONB(), nullable=True))
    op.create_table(
        "catalog_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("url", sa.String(400), nullable=False),
        sa.Column("fetched_at", TS, nullable=False),
        sa.Column("published_at", TS, nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("content_type", sa.String(64), nullable=True),
        sa.Column("licence", sa.String(64), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("body", sa.LargeBinary(), nullable=False),
        sa.UniqueConstraint("source", "name", "version", name="uq_catalog_snapshot_version"),
    )
    op.create_index("ix_catalog_snapshots_source_time", "catalog_snapshots", ["source", "fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_catalog_snapshots_source_time", table_name="catalog_snapshots")
    op.drop_table("catalog_snapshots")
    for c in ("corroboration", "attribution", "licence", "basis"):
        op.drop_column("archived_prices", c)
