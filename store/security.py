"""Security tables (migration 0013_security). See methodology/security.md.

    api_key_lineage   one row per API key created since 0013: which key created it (parent_key_id,
                      NULL = the operator / CLI), and whether it is a platform-admin key (the only
                      kind of API key whose `admin` scope is honoured). Kept beside api_keys rather
                      than as new api_keys columns so keys issued before 0013 keep working unchanged
                      (no row = no parent, not platform admin).
    security_events   append-only audit of security-relevant changes: key created / revoked
                      (including cascades to child keys), platform-admin grants, credential
                      re-encryption. Never holds a secret.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from tables import Base


class ApiKeyLineage(Base):
    __tablename__ = "api_key_lineage"
    __table_args__ = (Index("ix_api_key_lineage_parent", "parent_key_id"),)

    key_id: Mapped[int] = mapped_column(ForeignKey("api_keys.id", ondelete="CASCADE"), primary_key=True)
    parent_key_id: Mapped[int | None] = mapped_column(Integer)          # NULL: created by operator / CLI
    platform_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_by: Mapped[str] = mapped_column(String(16), default="api_key")   # operator | api_key | cli
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_security_events_ts", "ts"), Index("ix_security_events_account", "account_id", "ts"))

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    kind: Mapped[str] = mapped_column(String(48))            # key_created | key_revoked | ...
    actor: Mapped[str] = mapped_column(String(64))           # operator | key:<id> | cli | system
    account_id: Mapped[int | None] = mapped_column(Integer)
    key_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
