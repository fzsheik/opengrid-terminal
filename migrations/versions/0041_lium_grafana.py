"""lium grafana: rental starts and hourly series read from Lium's public Grafana dashboards, internal by default
(lium_data/grafana.py; store/lium.py; methodology/lium-data.md). Additive only: two new tables.

Revision ID: 0041_lium_grafana
Revises: 0040_lium_data
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0041_lium_grafana"
down_revision: Union[str, Sequence[str], None] = "0040_lium_data"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
VIS = "visibility IN ('public', 'internal')"


def _vis(table: str):
    return (sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
            sa.CheckConstraint(VIS, name=f"ck_{table}_visibility"))


def upgrade() -> None:
    op.create_table(
        "lium_grafana_rentals",
        sa.Column("row_key", sa.String(32), primary_key=True),
        sa.Column("start_at", TS, nullable=False),
        sa.Column("gpu_model", sa.String(128), nullable=False),
        sa.Column("price_per_gpu_hour", sa.Numeric(14, 6), nullable=False),
        sa.Column("tier", sa.String(16)),
        sa.Column("snapshot_id", sa.BigInteger, sa.ForeignKey("lium_feed_snapshots.id", ondelete="SET NULL")),
        *_vis("lium_grafana_rentals"),
    )
    op.create_index("ix_lium_grafana_rentals_start", "lium_grafana_rentals", ["start_at"])

    op.create_table(
        "lium_grafana_hourly",
        sa.Column("hour_utc", TS, primary_key=True),
        sa.Column("gpu_model", sa.String(128), primary_key=True),
        sa.Column("spend_usd_per_hour", sa.Numeric(16, 6)),
        sa.Column("gpus_listed", sa.Numeric(14, 6)),
        sa.Column("gpus_rented", sa.Numeric(14, 6)),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_grafana_hourly"),
    )


def downgrade() -> None:
    op.drop_table("lium_grafana_hourly")
    op.drop_table("lium_grafana_rentals")
