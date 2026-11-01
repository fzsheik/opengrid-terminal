"""stream_events: change-only events from push feeds, with the provider's timestamp (live/; methodology/live-feeds.md).
Additive only: one new table. Listings still come from raw_snapshots through the providers' normalizers.

Revision ID: 0031_stream_events
Revises: 0026_fx
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0031_stream_events"
down_revision: Union[str, Sequence[str], None] = "0026_fx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "stream_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("stream", sa.String(64), nullable=False),
        sa.Column("connection_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=True),
        sa.Column("provider_ts", TS, nullable=False),
        sa.Column("received_at", TS, nullable=False),
        sa.Column("prev_seen_at", TS, nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("item_id", sa.String(256), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(), nullable=True),
        sa.Column("kept", postgresql.JSONB(), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_stream_events_provider_item_ts", "stream_events", ["provider", "item_id", "provider_ts"])
    op.create_index("ix_stream_events_provider_ts", "stream_events", ["provider", "provider_ts"])


def downgrade() -> None:
    op.drop_index("ix_stream_events_provider_ts", table_name="stream_events")
    op.drop_index("ix_stream_events_provider_item_ts", table_name="stream_events")
    op.drop_table("stream_events")
