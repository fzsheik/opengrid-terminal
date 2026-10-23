"""provider incidents: what each provider's own status page reported (uptime/incidents.py; methodology/uptime.md).
Additive only.

    provider_incidents  one row per (provider, incident id): title, impact, status, start / resolve times, the update
                        text, affected components or products, and an inferred scope (global / regional /
                        unspecified) with the regions the incident names

Revision ID: 0023_incidents
Revises: 0022_uptime
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0023_incidents"
down_revision: Union[str, Sequence[str], None] = "0022_uptime"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "provider_incidents",
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("external_id", sa.String(96), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("impact", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("resolved_at", TS, nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("scope_basis", sa.Text(), nullable=True),
        sa.Column("regions", J, nullable=False, server_default="[]"),
        sa.Column("components", J, nullable=False, server_default="[]"),
        sa.Column("updates", J, nullable=False, server_default="[]"),
        sa.Column("first_seen_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("provider", "external_id"),
    )
    op.create_index("ix_provider_incidents_started", "provider_incidents", ["provider", "started_at"])


def downgrade() -> None:
    op.drop_table("provider_incidents")
