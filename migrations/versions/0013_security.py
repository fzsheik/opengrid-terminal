"""security hardening (store/security.py)

Revision ID: 0013_security
Revises: 0012_metrics
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0013_security"
down_revision: Union[str, Sequence[str], None] = "0012_metrics"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_key_lineage",
        sa.Column("key_id", sa.Integer(), sa.ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("parent_key_id", sa.Integer(), nullable=True),
        sa.Column("platform_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_api_key_lineage_parent", "api_key_lineage", ["parent_key_id"])
    op.create_table(
        "security_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("detail", postgresql.JSONB(), nullable=True),
    )
    op.create_index("ix_security_events_ts", "security_events", ["ts"])
    op.create_index("ix_security_events_account", "security_events", ["account_id", "ts"])


def downgrade() -> None:
    op.drop_index("ix_security_events_account", table_name="security_events")
    op.drop_index("ix_security_events_ts", table_name="security_events")
    op.drop_table("security_events")
    op.drop_index("ix_api_key_lineage_parent", table_name="api_key_lineage")
    op.drop_table("api_key_lineage")
