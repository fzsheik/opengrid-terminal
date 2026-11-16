"""lium grafana history: whole-network rentals created and deploy failures per hour, daily asking and paid medians
per GPU model, daily failure causes and penalty reasons, read from Lium's public Grafana and internal by default
(lium_data/grafana.py; store/lium.py; methodology/lium-data.md). Additive only: three new tables.

Revision ID: 0043_lium_grafana_history
Revises: 0042_operator_daily
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0043_lium_grafana_history"
down_revision: Union[str, Sequence[str], None] = "0042_operator_daily"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
VIS = "visibility IN ('public', 'internal')"


def _vis(table: str):
    return (sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
            sa.CheckConstraint(VIS, name=f"ck_{table}_visibility"))


def upgrade() -> None:
    op.create_table(
        "lium_grafana_network_hourly",
        sa.Column("hour_utc", TS, primary_key=True),
        sa.Column("rentals_created", sa.Integer),
        sa.Column("node_deploy_failures", sa.Integer),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_grafana_network_hourly"),
    )
    op.create_table(
        "lium_grafana_price_daily",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("gpu_model", sa.String(128), primary_key=True),
        sa.Column("median_asking", sa.Numeric(14, 6)),
        sa.Column("median_paid", sa.Numeric(14, 6)),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_grafana_price_daily"),
    )
    op.create_table(
        "lium_grafana_daily_counts",
        sa.Column("day", sa.Date, primary_key=True),
        sa.Column("series", sa.String(32), primary_key=True),
        sa.Column("label", sa.String(128), primary_key=True),
        sa.Column("count", sa.Integer, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        *_vis("lium_grafana_daily_counts"),
    )


def downgrade() -> None:
    op.drop_table("lium_grafana_daily_counts")
    op.drop_table("lium_grafana_price_daily")
    op.drop_table("lium_grafana_network_hourly")
