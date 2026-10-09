from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Absolute, so the `opengrid` launcher works from any directory.
    # extra="ignore": .env may hold variables this app doesn't use.
    model_config = SettingsConfigDict(env_file=Path(__file__).parent / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://localhost:5432/opengrid"
    # HTTP basic auth for the whole site. Unset means open, which is only right on your own machine.
    app_user: str = "opengrid"
    app_password: str | None = None
    # Open the read-only public surface (pages, /v1 data reads) without the site password.
    public_pages: bool = False
    # Real provisioning spends money. Off unless explicitly enabled per environment.
    routing_live_provisioning: bool = False
    # Public base URL, for canonical links and sitemaps.
    public_base_url: str = "https://opengrid-terminal-production.up.railway.app"
    # Poll public news / regulatory feeds (news/sources.py) in the background.
    news_enabled: bool = True
    # OpenGrid API keys are stored only as HMAC-SHA256(pepper, key). Required when deployed;
    # changing it invalidates every issued key. Unset in dev: a derived dev pepper, with a warning.
    api_key_pepper: str | None = None
    # Fernet key (or any passphrase, stretched) that encrypts BYO provider credentials and webhook
    # secrets at rest. Required when deployed; losing it makes stored BYO credentials unreadable.
    credentials_encryption_key: str | None = None
    # Per-minute request budgets per API key, by request class (a key's own override replaces `read`).
    rate_limit_read_per_minute: int = 120
    rate_limit_write_per_minute: int = 30
    rate_limit_execute_per_minute: int = 10
    # How long per-request API key usage rows are kept.
    api_usage_retention_days: int = 90
    # Alert webhooks to private / loopback addresses are refused unless this is set (dev only).
    alerts_webhook_allow_private: bool = False
    # ---- security (methodology/security.md) ----
    # 'development' | 'production'. deploy/make_env.py writes ENVIRONMENT=production. The app counts as
    # DEPLOYED (pepper, password and a real Fernet key required) if ANY of: this is 'production',
    # RAILWAY_ENVIRONMENT is set, or DATABASE_URL points at a host other than localhost.
    environment: str = "development"
    # Old Fernet keys, comma-separated, still accepted for DECRYPTION while rotating
    # credentials_encryption_key (MultiFernet). Run `python admin.py rotate-credentials`, then drop them.
    credentials_encryption_keys_old: str | None = None
    # Trust X-Forwarded-For (Railway's edge sets it). trusted_proxy_count = how many proxies append to it;
    # the client is the entry that many hops from the right. Off: request.client.host only.
    trust_proxy_headers: bool = False
    trusted_proxy_count: int = 1
    # Self-service keys: at most this many active keys per account.
    max_keys_per_account: int = 20
    # Per-ACCOUNT budgets per request class, on top of each key's own bucket (so extra keys add
    # nothing). An account's settings JSON may override with {"rate_limit_account": {"read": n, ...}}.
    account_rate_limit_read_per_minute: int = 600
    account_rate_limit_write_per_minute: int = 120
    account_rate_limit_execute_per_minute: int = 20
    # Anonymous visitors when PUBLIC_PAGES is on: requests per minute per client IP (static assets free).
    public_rate_limit_per_minute: int = 240
    # Failed basic-auth logins per client IP within login_failure_window_seconds before a 429 lockout.
    login_max_failures: int = 10
    login_failure_window_seconds: int = 300
    # Poll providers in the background. Off for read-only dev servers against a shared DB.
    poller_enabled: bool = True
    # Ops alerts (alerts/ops.py): orphans, termination failures, ambiguous launches, kill switches.
    # Any HTTPS webhook (Slack/PagerDuty incoming webhooks work); signed with the secret.
    ops_alert_webhook_url: str | None = None
    ops_alert_webhook_secret: str | None = None
    # Public logo shown as the alert sender's avatar (Discord fetches it; must be publicly reachable).
    ops_alert_logo_url: str = "https://tryopengrid.com/brand/opengrid-logo.png"
    # Channel feeds (alerts/channels.py): one webhook per channel, signed with OPS_ALERT_WEBHOOK_SECRET.
    deployments_webhook_url: str | None = None   # lifecycle: awaiting approval, running, terminated, failed
    market_webhook_url: str | None = None        # notable / major market events
    news_webhook_url: str | None = None          # high-relevance news
    news_post_min_relevance: int = 50
    salad_api_key: str | None = None
    salad_org: str | None = None
    hyperstack_api_key: str | None = None
    lambda_api_key: str | None = None
    runpod_api_key: str | None = None
    digitalocean_api_key: str | None = None
    # Routing (execution) credentials, OpenGrid-managed. Each enables one adapter's live calls.
    shadeform_api_key: str | None = None      # one key launches Crusoe / Denvr / Latitude via Shadeform
    vast_api_key: str | None = None
    verda_client_id: str | None = None
    verda_client_secret: str | None = None
    # Per-provider launch defaults (JSON), e.g. {"lambda": {"ssh_key": "opengrid"},
    # "hyperstack": {"ssh_key": "k", "image": "Ubuntu Server 22.04 LTS R535 CUDA 12.2",
    # "environments": {"CANADA-1": "default-CANADA-1"}}}. See routing/adapters/*.py.
    routing_launch_defaults: dict = {}
    # How many provision calls one route may make. Failover happens only after a DEFINITIVE provider
    # rejection, never after an ambiguous outcome; 1 = no failover at all (this pass's default).
    routing_max_attempts: int = 1
    # --- execution core (routing/control.py, quotes.py, guards.py; methodology/execution-safety.md) ---
    quote_ttl_seconds: int = 300                 # a quote expires this long after it is issued
    quote_price_tolerance: float = 0.02          # re-validation refuses a launch if the price moved more
    validation_max_price_per_hour: float = 3.0   # validation launch: total instance $/h cap
    validation_max_runtime_minutes: int = 30     # validation launch: auto-terminate deadline
    default_max_runtime_minutes: int | None = None   # DEPRECATED (ignored since 0014_limits): see runtime_* below
    # Runtime ceilings (routing/guards.runtime_ceiling; 0014_limits). Every deployment gets an auto-terminate
    # deadline: effective = min(hard max, account max, request | account default | system default). Never unlimited.
    runtime_hard_max_minutes: int = 1440         # system hard max (24 h); requests above are clamped
    runtime_default_minutes: int = 60            # when neither the request nor the account sets one
    # Validation launch gate (routing/validation.preconditions): providers allowed for a validation launch.
    validation_allowed_providers: list[str] = ["lambda"]
    validation_drill_window_days: int = 7        # kill-switch drills / ops test alert must be this recent
    default_max_price_per_gpu_hour: float | None = None
    default_max_hourly_cost: float = 50.0
    default_max_total_cost: float | None = None
    default_max_gpus: int = 8                    # concurrent GPUs across an account's active deployments
    default_max_active_deployments: int = 2
    default_monthly_spend_limit: float = 2000.0
    route_live_check_candidates: int = 3         # live availability checks per route, top-N only
    provider_call_timeout_seconds: float = 20.0  # per provider call during a route (checks)
    # --- reconciliation & metering (routing/reconcile.py, tracker.py; methodology/reconciliation.md) ---
    reconcile_interval_seconds: int = 120        # reconciliation job period
    provisioning_timeout_minutes: int = 15       # stale provisioning / unknown launches resolved by name after this
    not_found_confirm_seconds: int = 60          # two not_found reads must be at least this far apart
    termination_retry_max: int = 5               # terminate re-issues before termination_failed + alert
    termination_retry_base_seconds: int = 60     # backoff base: base * 2^attempt between terminate retries
    tracker_interval_seconds: int = 60           # status polling + usage metering job period
    # --- lifecycle: cost-exposure alerts, provider resources (alerts/ops.py, routing/adapters/resources.py) ---
    alert_unknown_minutes: int = 10              # resource state unknown longer than this -> ops alert
    alert_overspend_pct: float = 20.0            # spend above the quote by more than this % -> ops alert
    alert_reescalate_minutes: int = 30           # an unresolved cost-exposure alert is re-sent this often
    ssh_key_delete_retry_max: int = 5            # provider key deletes before delete_failed + ops alert
    ssh_key_delete_retry_base_seconds: int = 60  # backoff base between key delete retries (x 2^attempt)
    idempotency_ttl_hours: int = 24
    idempotency_stale_seconds: int = 600         # an in_progress key older than this may be reclaimed
    # Observability (observability.py). JSON log lines; None = JSON when deployed (RAILWAY_ENVIRONMENT), text in dev.
    log_json: bool | None = None
    # Provider reliability (routing/reliability.py): no score below this many real transactions.
    reliability_min_samples: int = 10
    # Product analytics (analytics/product.py): POST /v1/events/track budget per client per minute.
    product_events_per_minute: int = 60
    # How often the route_outcomes / product server-events jobs recompute (seconds).
    metrics_recompute_seconds: int = 300

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, url: str) -> str:
        """Hosts hand out postgres:// or postgresql://; SQLAlchemy needs the driver named."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
