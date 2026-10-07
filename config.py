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
    # Poll providers in the background. Off for read-only dev servers against a shared DB.
    poller_enabled: bool = True
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
    # How many ranked, provisionable candidates /v1/route tries before giving up.
    routing_max_attempts: int = 3

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, url: str) -> str:
        """Hosts hand out postgres:// or postgresql://; SQLAlchemy needs the driver named."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
