"""reservations: reserved-compute price-lock requests (store/reservations.py): reservation_requests,
reservation_events. Additive only.

Revision ID: 0019_reservations
Revises: 0018_settlement
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0019_reservations"
down_revision: Union[str, Sequence[str], None] = "0018_settlement"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "reservation_requests",
        sa.Column("request_id", sa.String(40), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("principal_kind", sa.String(16), nullable=True),
        sa.Column("gpu", sa.String(128), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("total_gpu_hours", sa.Numeric(14, 2), nullable=False),
        sa.Column("start_after", TS, nullable=False),
        sa.Column("start_before", TS, nullable=False),
        sa.Column("region_group", sa.String(32), nullable=True),
        sa.Column("max_price_per_gpu_hour", sa.Numeric(12, 6), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("indicative", J, nullable=True),
        sa.Column("offer_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("offer_price_per_gpu_hour", sa.Numeric(12, 6), nullable=True),
        sa.Column("offer_valid_until", TS, nullable=True),
        sa.Column("offer_terms", sa.Text(), nullable=True),
        sa.Column("offer_supply_basis", sa.Text(), nullable=True),
        sa.Column("offered_at", TS, nullable=True),
        sa.Column("offered_by", sa.String(64), nullable=True),
        sa.Column("accepted_at", TS, nullable=True),
        sa.Column("accepted_by", sa.String(64), nullable=True),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("state_changed_at", TS, nullable=True),
        # An offer without a recorded supply basis cannot exist, whatever code path wrote it.
        sa.CheckConstraint("offer_price_per_gpu_hour IS NULL OR "
                           "(offer_supply_basis IS NOT NULL AND length(btrim(offer_supply_basis)) > 0)",
                           name="ck_rsv_offer_has_supply_basis"),
    )
    op.create_index("ix_rsv_account_time", "reservation_requests", ["account_id", "created_at"])
    op.create_index("ix_rsv_status", "reservation_requests", ["status"])

    op.create_table(
        "reservation_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, primary_key=True),
        sa.Column("request_id", sa.String(40), nullable=False),
        sa.Column("at", TS, nullable=False),
        sa.Column("from_status", sa.String(24), nullable=True),
        sa.Column("to_status", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("evidence", J, nullable=True),
    )
    op.create_index("ix_rsv_events_request", "reservation_events", ["request_id", "at"])
    op.create_index("ix_rsv_events_at", "reservation_events", ["at"])


def downgrade() -> None:
    op.drop_index("ix_rsv_events_at", table_name="reservation_events")
    op.drop_index("ix_rsv_events_request", table_name="reservation_events")
    op.drop_table("reservation_events")
    op.drop_index("ix_rsv_status", table_name="reservation_requests")
    op.drop_index("ix_rsv_account_time", table_name="reservation_requests")
    op.drop_table("reservation_requests")
