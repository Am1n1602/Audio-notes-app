from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]  # backend/app/core/config.py -> repo root


class Settings(BaseSettings):
    """One settings object for the API and the worker. Real environment variables win over the root .env."""

    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    database_url: str
    db_connect_timeout_seconds: int = Field(default=3, ge=1)  # a Neon database asleep after 5 idle minutes needs ~10

    # How a job's next step is delivered. "celery": Redis + a Celery worker (local development). "cloudtasks": Google
    # Cloud Tasks calls the private step endpoint (production, nothing always-on). Everything else is the same code.
    queue_backend: Literal["celery", "cloudtasks"] = "celery"
    redis_url: str = ""  # celery only
    # What this process serves: "api" is the public /api/*; "steps" is the private POST /internal/steps that Cloud
    # Tasks calls. Two deployments of one image, because Cloud Run grants access per service, not per path.
    service_role: Literal["api", "steps"] = "api"
    cloud_tasks_queue: str = ""  # projects/<project>/locations/<region>/queues/<queue>
    steps_url: str = ""  # the steps service's own URL, with no path: Cloud Tasks posts to <it>/internal/steps
    steps_invoker_email: str = ""  # the service account Cloud Tasks signs its call as (it holds run.invoker on steps)
    steps_dispatch_deadline_seconds: int = (
        300  # Cloud Tasks waits this long for a step to answer (a step takes <=215 s)
    )
    max_uploads_per_day: int = Field(default=0, ge=0)  # per browser, over the last 24 hours; 0 means no limit

    # Object storage (private S3 bucket). Required: the API cannot do anything useful without it.
    storage_region: str
    storage_bucket: str
    storage_access_key_id: str
    storage_secret_access_key: SecretStr  # SecretStr so it never shows up in a repr or log line
    storage_endpoint: str = ""  # only for non-AWS S3-compatible stores; empty means AWS

    # A single presigned PUT can carry at most 5 GB; 2 GiB leaves headroom and is far above 4 h of audio.
    max_upload_bytes: int = 2 * 1024**3
    upload_url_expires_seconds: int = 900  # S3 checks expiry when the request starts, not when it ends
    audio_url_expires_seconds: int = 3600  # the signed link the player uses; the page asks for a new one when it lapses
    # A job none of whose steps has run for this long is revived when its owner's page next asks (services/recovery.py).
    # Every step touches the job, and the longest honest silence is one summary step's retries (about 3.5 minutes).
    job_stalled_after_seconds: int = 600

    # Gnani Batch STT (used by the worker; the API never calls Gnani). Optional here so the API process can run
    # without provider secrets it never uses; the worker refuses to start without them (missing_worker_secrets).
    gnani_api_key: SecretStr | None = None
    gnani_base_url: str = "https://api.vachana.ai"
    gnani_poll_seconds: int = Field(default=10, ge=10)  # Gnani asks for no faster than one status poll per 10 s
    # Measured live: calls 1 s apart all succeed, calls 0.5 s apart are refused (429) half the time. So the client
    # spaces its own calls at least this far apart (with a margin) instead of collecting 429s.
    gnani_min_call_interval_seconds: float = Field(default=1.2, ge=0)
    gnani_max_transcribe_seconds: int = 7200  # stop waiting on a job Gnani never finishes (fails as a timeout)
    gnani_source_url_expires_seconds: int = 3600  # lifetime of the signed download link we hand to Gnani

    # Summary LLM (Groq's OpenAI-compatible API; used by the worker, never by the API).
    llm_api_key: SecretStr | None = None
    # No hidden reasoning tokens, unlike gpt-oss (which spends ~1000 of every answer's token budget thinking).
    # llama-3.3-70b-versatile is not available to every key. Any chat model id works.
    llm_model: str = "qwen/qwen3.8-27b"
    llm_base_url: str = "https://api.groq.com/openai/v1"
    # Groq limits OUTPUT tokens per minute separately from total tokens: 1000 on our key (found in a 429 body: "on
    # output tokens per minute (OTPM): Limit 1000"), so an answer longer than that can never succeed. The prompts ask
    # for about 200 words (~650 tokens of Hindi, ~300 of English); this cap only stops a runaway answer, which is then
    # retried once with a request to be much shorter.
    llm_max_output_tokens: int = 900
    # A transcript longer than this is summarised in parts and the parts merged. Groq caps total tokens per MINUTE per
    # model (8000 on our key, from the x-ratelimit-limit-tokens header) and OUTPUT tokens per minute (1000, above).
    # Measured: a 6000-character part of number-heavy English cost ~2100 prompt tokens (prompt included), and Hindi
    # costs more (about 2.4 characters per token). With short part answers a call is ~2.5K tokens in and a few hundred
    # out, so parts 30 s apart stay under both caps. Raise the size and shorten the interval on a key with higher
    # limits.
    llm_chunk_chars: int = 6000
    llm_chunk_interval_seconds: int = 30  # pause between part-summaries so the tokens-per-minute cap holds
    llm_max_wait_seconds: int = 30  # longest 429 "retry-after" we wait for inside a step; longer fails as rate-limited

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

    @model_validator(mode="after")
    def queue_backend_is_configured(self) -> Self:
        wanted = (
            {"REDIS_URL": self.redis_url}
            if self.queue_backend == "celery"
            else {
                "CLOUD_TASKS_QUEUE": self.cloud_tasks_queue,
                "STEPS_URL": self.steps_url,
                "STEPS_INVOKER_EMAIL": self.steps_invoker_email,
            }
        )
        if missing := [name for name, value in wanted.items() if not value.strip()]:
            raise ValueError(f"QUEUE_BACKEND={self.queue_backend} needs {', '.join(missing)}")
        return self

    def missing_worker_secrets(self) -> list[str]:
        """Env var names the worker cannot run without (unset or blank). The API never needs them."""
        wanted = (("GNANI_API_KEY", self.gnani_api_key), ("LLM_API_KEY", self.llm_api_key))
        return [name for name, value in wanted if value is None or not value.get_secret_value().strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


def require_secret(value: SecretStr | None, name: str) -> str:
    """The secret's text, or a clear error naming the variable (never its value)."""
    if value is None or not value.get_secret_value().strip():
        raise RuntimeError(f"{name} is not set (it is required by the worker)")
    return value.get_secret_value()


@lru_cache
def get_settings() -> Settings:
    return Settings()
