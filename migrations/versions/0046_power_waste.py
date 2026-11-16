"""power waste: gas flared at oil and gas sites (power/flaring.py; methodology/power-waste.md). Curtailment needs no
table of its own (power_series / power_days). Additive only: three new tables.

    power_flare_sites     a flare site with coordinates (satellite detection cell or regulator location)
    power_flare_volumes   flared volume per site and year (+ the months, where the source is monthly)
    power_flare_areas     regulator volumes per county (or state) and month / year

Revision ID: 0046_power_waste
Revises: 0045_power_sites
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0046_power_waste"
down_revision: Union[str, Sequence[str], None] = "0045_power_sites"  # re-chained at merge
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
VIS = "visibility IN ('public', 'internal')"


def upgrade() -> None:
    op.create_table(
        "power_flare_sites",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("site_key", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(32), nullable=True),
        sa.Column("country", sa.String(3), nullable=True),
        sa.Column("country_name", sa.String(80), nullable=True),
        sa.Column("state", sa.String(8), nullable=True),
        sa.Column("county", sa.String(64), nullable=True),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lon", sa.Float(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("source", "site_key", name="uq_power_flare_sites_source_key"),
        sa.CheckConstraint(VIS, name="ck_power_flare_sites_visibility"),
    )
    op.create_index("ix_power_flare_sites_lat_lon", "power_flare_sites", ["lat", "lon"])
    op.create_index("ix_power_flare_sites_country_state", "power_flare_sites", ["country", "state"])
    op.create_table(
        "power_flare_volumes",
        sa.Column("site_id", sa.Integer(), sa.ForeignKey("power_flare_sites.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("year", sa.SmallInteger(), primary_key=True),
        sa.Column("volume_m3", sa.Float(), nullable=True),
        sa.Column("volume_mcf", sa.Float(), nullable=True),
        sa.Column("monthly_mcf", postgresql.ARRAY(sa.REAL()), nullable=True),
        sa.Column("months_reported", sa.SmallInteger(), nullable=True),
        sa.Column("detections", sa.Integer(), nullable=True),
        sa.Column("energy_mwh_derived", sa.Float(), nullable=True),
        sa.Column("fetch_id", sa.BigInteger(), sa.ForeignKey("power_fetches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_power_flare_volumes_year", "power_flare_volumes", ["year"])
    op.create_table(
        "power_flare_areas",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("area_type", sa.String(16), primary_key=True),
        sa.Column("country", sa.String(3), primary_key=True),
        sa.Column("state", sa.String(8), primary_key=True),
        sa.Column("area", sa.String(64), primary_key=True),
        sa.Column("period", sa.Date(), primary_key=True),
        sa.Column("period_type", sa.String(8), primary_key=True),
        sa.Column("volume_mcf", sa.Float(), nullable=True),
        sa.Column("gas_produced_mcf", sa.Float(), nullable=True),
        sa.Column("wells", sa.Integer(), nullable=True),
        sa.Column("energy_mwh_derived", sa.Float(), nullable=True),
        sa.Column("meta", postgresql.JSONB(), nullable=True),
        sa.Column("fetch_id", sa.BigInteger(), sa.ForeignKey("power_fetches.id", ondelete="SET NULL"), nullable=True),
        sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(VIS, name="ck_power_flare_areas_visibility"),
    )
    op.create_index("ix_power_flare_areas_period", "power_flare_areas", ["period"])


def downgrade() -> None:
    op.drop_table("power_flare_areas")
    op.drop_table("power_flare_volumes")
    op.drop_table("power_flare_sites")
