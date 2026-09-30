from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/core/config.py -> repo root


class Settings(BaseSettings):
    """One settings object for the API and the worker. Real environment variables win over the root .env."""

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str
    redis_url: str

    # Object storage (private S3 bucket). Required: the API cannot do anything useful without it.
    storage_region: str
    storage_bucket: str
    storage_access_key_id: str
    storage_secret_access_key: SecretStr  # SecretStr so it never shows up in a repr or log line
    storage_endpoint: str = ""  # only for non-AWS S3-compatible stores; empty means AWS

    # A single presigned PUT can carry at most 5 GB; 2 GiB leaves headroom and is far above 4 h of audio.
    max_upload_bytes: int = 2 * 1024**3
    upload_url_expires_seconds: int = 900  # S3 checks expiry when the request starts, not when it ends

    cors_origins: str = ""  # comma-separated list of allowed browser origins
    app_env: str = "development"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def use_psycopg3_driver(cls, url: str) -> str:
        # Managed Postgres hands out postgres:// or postgresql:// URLs, which SQLAlchemy maps to the
        # psycopg2 driver. We ship psycopg 3, so pin the dialect instead of failing at deploy time.
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+psycopg://" + url[len(prefix) :]
        return url

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
