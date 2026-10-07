"""accounts, api keys, usage, billing, watchlists (store/accounts.py)

Revision ID: 0007_accounts
Revises: 0006_news
"""
from typing import Sequence, Union

from alembic import op  # noqa: F401
import sqlalchemy as sa  # noqa: F401
from sqlalchemy.dialects import postgresql  # noqa: F401

revision: str = "0007_accounts"
down_revision: Union[str, Sequence[str], None] = "0006_news"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TZ = sa.DateTime(timezone=True)
MONEY = sa.Numeric(14, 6)
NOW = sa.text("now()")
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("plan", sa.String(32), nullable=False),
        sa.Column("settings", JSONB, nullable=False),
        sa.Column("is_operator", sa.Boolean(), nullable=False),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ux_accounts_operator", "accounts", ["is_operator"], unique=True,
                    postgresql_where=sa.text("is_operator"))

    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("prefix", sa.String(24), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
        sa.Column("last_used_at", TZ, nullable=True),
        sa.Column("last_used_ip", sa.String(64), nullable=True),
        sa.Column("expires_at", TZ, nullable=True),
        sa.Column("revoked_at", TZ, nullable=True),
        sa.Column("rate_limit_per_minute", sa.Integer(), nullable=True),
    )
    op.create_index("ix_api_keys_account", "api_keys", ["account_id"])

    op.create_table(
        "api_key_usage",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("key_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("ts", TZ, nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("status", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_key_usage_account_ts", "api_key_usage", ["account_id", "ts"])
    op.create_index("ix_key_usage_key_ts", "api_key_usage", ["key_id", "ts"])
    op.create_index("ix_key_usage_ts", "api_key_usage", ["ts"])

    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("secret_encrypted", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.String(16), nullable=True),
        sa.Column("label", sa.String(200), nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
        sa.Column("last_used_at", TZ, nullable=True),
        sa.Column("revoked_at", TZ, nullable=True),
    )
    op.create_index("ix_provider_credentials_account", "provider_credentials", ["account_id", "provider"])

    # ---- billing
    op.create_table(
        "usage_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("deployment_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=False),
        sa.Column("gpu_count", sa.Integer(), nullable=False),
        sa.Column("period_start", TZ, nullable=False),
        sa.Column("period_end", TZ, nullable=False),
        sa.Column("gpu_hours", MONEY, nullable=False),
        sa.Column("provider_cost_usd", MONEY, nullable=False),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
        sa.UniqueConstraint("deployment_id", "period_start", "period_end", name="uq_usage_deployment_period"),
    )
    op.create_index("ix_usage_records_account_period", "usage_records", ["account_id", "period_start"])

    op.create_table(
        "fee_policies",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("effective_from", TZ, nullable=False),
        sa.Column("effective_to", TZ, nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_fee_policies_scope", "fee_policies", ["account_id", "effective_from"])

    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),
        sa.Column("period_start", TZ, nullable=False),
        sa.Column("period_end", TZ, nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("subtotal_usd", MONEY, nullable=False),
        sa.Column("credits_usd", MONEY, nullable=False),
        sa.Column("total_usd", MONEY, nullable=False),
        sa.Column("totals", JSONB, nullable=False),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
        sa.Column("updated_at", TZ, server_default=NOW, nullable=False),
        sa.UniqueConstraint("account_id", "period", name="uq_invoices_account_period"),
    )

    op.create_table(
        "charges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("usage_record_id", sa.Integer(), sa.ForeignKey("usage_records.id", ondelete="CASCADE"), nullable=True),
        sa.Column("invoice_id", sa.Integer(), sa.ForeignKey("invoices.id", ondelete="SET NULL"), nullable=True),
        sa.Column("credit_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("amount_usd", MONEY, nullable=False),
        sa.Column("policy_id", sa.Integer(), nullable=True),
        sa.Column("component", JSONB, nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_charges_account_created", "charges", ["account_id", "created_at"])
    op.create_index("ix_charges_invoice", "charges", ["invoice_id"])
    op.create_index("ix_charges_usage", "charges", ["usage_record_id"])

    op.create_table(
        "credits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("amount_usd", MONEY, nullable=False),
        sa.Column("remaining_usd", MONEY, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expires_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_credits_account", "credits", ["account_id"])

    # ---- watchlists and alerts
    op.create_table(
        "watchlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_watchlists_account", "watchlists", ["account_id"])

    op.create_table(
        "watchlist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("watchlist_id", sa.Integer(), sa.ForeignKey("watchlists.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("gpu", sa.String(160), nullable=True),
        sa.Column("provider", sa.String(64), nullable=True),
        sa.Column("region_group", sa.String(32), nullable=True),
        sa.Column("index_id", sa.String(64), nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_watchlist_items_list", "watchlist_items", ["watchlist_id"])

    op.create_table(
        "alert_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("params", JSONB, nullable=False),
        sa.Column("channels", JSONB, nullable=False),
        sa.Column("webhook_secret_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("cooldown_seconds", sa.Integer(), nullable=False),
        sa.Column("last_evaluated_at", TZ, nullable=True),
        sa.Column("last_state", sa.String(8), nullable=True),
        sa.Column("last_known_state", sa.String(8), nullable=True),
        sa.Column("last_value", sa.Float(), nullable=True),
        sa.Column("last_detail", sa.Text(), nullable=True),
        sa.Column("last_fired_at", TZ, nullable=True),
        sa.Column("created_at", TZ, server_default=NOW, nullable=False),
    )
    op.create_index("ix_alert_rules_account", "alert_rules", ["account_id"])
    op.create_index("ix_alert_rules_status", "alert_rules", ["status"])

    op.create_table(
        "alert_firings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("rule_id", sa.Integer(), sa.ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("fired_at", TZ, nullable=False),
        sa.Column("value", sa.Float(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("delivered_via", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("delivery_status", JSONB, nullable=False),
    )
    op.create_index("ix_alert_firings_account_time", "alert_firings", ["account_id", "fired_at"])
    op.create_index("ix_alert_firings_rule_time", "alert_firings", ["rule_id", "fired_at"])


def downgrade() -> None:
    for t in ("alert_firings", "alert_rules", "watchlist_items", "watchlists", "credits", "charges", "invoices",
              "fee_policies", "usage_records", "provider_credentials", "api_key_usage", "api_keys", "accounts"):
        op.drop_table(t)
