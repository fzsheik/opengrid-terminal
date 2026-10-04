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
    salad_api_key: str | None = None
    salad_org: str | None = None
    hyperstack_api_key: str | None = None
    lambda_api_key: str | None = None
    runpod_api_key: str | None = None
    digitalocean_api_key: str | None = None

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, url: str) -> str:
        """Hosts hand out postgres:// or postgresql://; SQLAlchemy needs the driver named."""
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix):]
        return url


settings = Settings()
