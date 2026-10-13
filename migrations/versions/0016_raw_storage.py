"""Content-addressed raw bodies; existing inline data is migrated in small batches."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0016_raw_storage"
down_revision = "0015_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("raw_payloads",
                    sa.Column("sha256", sa.String(64), primary_key=True),
                    sa.Column("payload", JSONB(), nullable=False))
    op.add_column("raw_snapshots", sa.Column("payload_hash", sa.String(64), nullable=True))
    op.create_foreign_key("fk_raw_payload_hash", "raw_snapshots", "raw_payloads", ["payload_hash"], ["sha256"])
    op.create_index("ix_raw_snapshots_payload_hash", "raw_snapshots", ["payload_hash"])
    op.create_index("ix_raw_inline_pending", "raw_snapshots", ["id"],
                    postgresql_where=sa.text("payload IS NOT NULL AND payload_hash IS NULL"))


def downgrade():
    # Restore inline bodies before removing the shared store.
    op.execute("UPDATE raw_snapshots r SET payload = p.payload FROM raw_payloads p WHERE r.payload_hash = p.sha256")
    op.drop_index("ix_raw_inline_pending", table_name="raw_snapshots")
    op.drop_index("ix_raw_snapshots_payload_hash", table_name="raw_snapshots")
    op.drop_constraint("fk_raw_payload_hash", "raw_snapshots", type_="foreignkey")
    op.drop_column("raw_snapshots", "payload_hash")
    op.drop_table("raw_payloads")
