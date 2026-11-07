"""api_keys.api_version: the Data API version a key is pinned to (docs/API_CONTRACT.md section 3). Additive.

Existing keys are pinned to the first version, 2026-11-03; new keys get the latest at creation (accounts/keys.py).

Revision ID: 0034_api_key_version
Revises: 0029_news_history
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0034_api_key_version"
down_revision: Union[str, Sequence[str], None] = "0029_news_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("api_version", sa.String(10), nullable=True))
    op.execute("UPDATE api_keys SET api_version = '2026-11-03' WHERE api_version IS NULL")


def downgrade() -> None:
    op.drop_column("api_keys", "api_version")
