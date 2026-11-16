"""power nodal: where power is stranded, node by node, without storing nodal interval prices (power/nodal.py;
methodology/power-markets.md). Additive only: two new tables.

    power_nodes       the nodal catalogue: ERCOT settlement points and SPP settlement locations, their type as the
                      source states it, zone and substation; county / latitude / longitude only where published
    power_node_days   one row per (market, day, node): intervals priced, below $0, below $20, mean, p10, p50,
                      min, max of that day's interval prices

Revision ID: 0047_power_nodal
Revises: 0046_power_waste
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0047_power_nodal"
down_revision: Union[str, Sequence[str], None] = "0046_power_waste"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "power_nodes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("iso", sa.String(16), nullable=False),
        sa.Column("node", sa.String(64), nullable=False),
        sa.Column("node_type", sa.String(24), nullable=True),
        sa.Column("zone", sa.String(32), nullable=True),
        sa.Column("substation", sa.String(64), nullable=True),
        sa.Column("county", sa.String(64), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("first_day", sa.Date(), nullable=True),
        sa.Column("last_day", sa.Date(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("iso", "node", name="uq_power_nodes_iso_node"),
    )
    op.create_table(
        "power_node_days",
        sa.Column("market", sa.String(2), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("node_id", sa.Integer(), sa.ForeignKey("power_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("n", sa.SmallInteger(), nullable=False),
        sa.Column("n_neg", sa.SmallInteger(), nullable=False),
        sa.Column("n_lt20", sa.SmallInteger(), nullable=False),
        sa.Column("mean", sa.REAL(), nullable=False),
        sa.Column("p10", sa.REAL(), nullable=False),
        sa.Column("p50", sa.REAL(), nullable=False),
        sa.Column("min", sa.REAL(), nullable=False),
        sa.Column("max", sa.REAL(), nullable=False),
        sa.PrimaryKeyConstraint("market", "day", "node_id"),
    )


def downgrade() -> None:
    op.drop_table("power_node_days")
    op.drop_table("power_nodes")
