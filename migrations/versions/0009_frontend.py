"""reserved (store/frontend.py)

Revision ID: 0009_frontend
Revises: 0008_routing
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0009_frontend"
down_revision: Union[str, Sequence[str], None] = "0008_routing"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
