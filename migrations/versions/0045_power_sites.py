"""power sites: the physical side of siting (power/sites/; methodology/power-markets.md "Physical sites").
Additive only: four new tables, nothing existing is touched (fetches go to power_fetches, dataset "sites.*").

    power_generators          EIA generator inventory, latest state per (plant, generator), with coordinates
    power_generator_history   change-only history of status, capacity and (planned) retirement / operation month
    power_plant_generation    annual net generation per plant (EIA-923)
    power_queue_projects      interconnection-queue projects per (source, edition, project id)

Revision ID: 0045_power_sites
Revises: 0043_lium_grafana_history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0045_power_sites"
down_revision: Union[str, Sequence[str], None] = "0043_lium_grafana_history"   # re-chained at merge
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def _fetch():
    return sa.Column("fetch_id", sa.BigInteger(), sa.ForeignKey("power_fetches.id", ondelete="SET NULL"),
                     nullable=True)


def upgrade() -> None:
    op.create_table(
        "power_generators",
        sa.Column("plant_id", sa.Integer(), primary_key=True),
        sa.Column("generator_id", sa.String(16), primary_key=True),
        sa.Column("plant_name", sa.String(200), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("entity_name", sa.String(200), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("county", sa.String(64), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("ba_code", sa.String(16), nullable=True),
        sa.Column("sector", sa.String(64), nullable=True),
        sa.Column("technology", sa.String(80), nullable=True),
        sa.Column("energy_source", sa.String(8), nullable=True),
        sa.Column("prime_mover", sa.String(8), nullable=True),
        sa.Column("nameplate_mw", sa.Float(), nullable=True),
        sa.Column("summer_mw", sa.Float(), nullable=True),
        sa.Column("winter_mw", sa.Float(), nullable=True),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("status_group", sa.String(16), nullable=False),
        sa.Column("operating_ym", sa.String(7), nullable=True),
        sa.Column("planned_operation_ym", sa.String(7), nullable=True),
        sa.Column("planned_retirement_ym", sa.String(7), nullable=True),
        sa.Column("retirement_ym", sa.String(7), nullable=True),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("via", sa.String(8), nullable=False),
        sa.Column("first_period", sa.Date(), nullable=False),
        _fetch(),
        sa.Column("updated_at", TS, nullable=False),
    )
    op.create_index("ix_power_generators_state_county", "power_generators", ["state", "county"])
    op.create_index("ix_power_generators_ba", "power_generators", ["ba_code"])
    op.create_table(
        "power_generator_history",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("plant_id", sa.Integer(), nullable=False),
        sa.Column("generator_id", sa.String(16), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("change", sa.String(8), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("status_group", sa.String(16), nullable=False),
        sa.Column("nameplate_mw", sa.Float(), nullable=True),
        sa.Column("summer_mw", sa.Float(), nullable=True),
        sa.Column("planned_operation_ym", sa.String(7), nullable=True),
        sa.Column("planned_retirement_ym", sa.String(7), nullable=True),
        sa.Column("retirement_ym", sa.String(7), nullable=True),
        sa.Column("via", sa.String(8), nullable=False),
        _fetch(),
        sa.UniqueConstraint("plant_id", "generator_id", "period", name="uq_power_generator_history"),
    )
    op.create_index("ix_power_generator_history_period", "power_generator_history", ["period"])
    op.create_table(
        "power_plant_generation",
        sa.Column("plant_id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.SmallInteger(), primary_key=True),
        sa.Column("net_mwh", sa.Float(), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        _fetch(),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("revisions", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_table(
        "power_queue_projects",
        sa.Column("source", sa.String(24), primary_key=True),
        sa.Column("edition", sa.Date(), primary_key=True),
        sa.Column("project_id", sa.String(160), primary_key=True),
        sa.Column("iso", sa.String(24), nullable=True),
        sa.Column("entity", sa.String(120), nullable=True),
        sa.Column("state", sa.String(2), nullable=True),
        sa.Column("county", sa.String(64), nullable=True),
        sa.Column("fips", sa.String(5), nullable=True),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("type", sa.String(48), nullable=True),
        sa.Column("type_detail", sa.String(120), nullable=True),
        sa.Column("mw", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("phase", sa.String(80), nullable=True),
        sa.Column("queue_date", sa.Date(), nullable=True),
        sa.Column("proposed_cod", sa.Date(), nullable=True),
        sa.Column("on_date", sa.Date(), nullable=True),
        sa.Column("withdrawn_date", sa.Date(), nullable=True),
        sa.Column("ia_date", sa.Date(), nullable=True),
        sa.Column("poi", sa.String(200), nullable=True),
        sa.Column("project_name", sa.String(200), nullable=True),
        sa.Column("developer", sa.String(200), nullable=True),
        sa.Column("visibility", sa.String(8), nullable=False, server_default="internal"),
        _fetch(),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint("visibility IN ('public', 'internal')", name="ck_power_queue_projects_visibility"),
    )
    op.create_index("ix_power_queue_projects_place", "power_queue_projects", ["state", "county"])


def downgrade() -> None:
    op.drop_table("power_queue_projects")
    op.drop_table("power_plant_generation")
    op.drop_table("power_generator_history")
    op.drop_table("power_generators")
