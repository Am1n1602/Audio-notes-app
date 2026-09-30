import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings, require_secret

REDIS = "redis://x"


def make(database_url: str) -> Settings:
    return Settings(_env_file=None, database_url=database_url, redis_url=REDIS)


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_managed_postgres_urls_are_pinned_to_psycopg3(scheme: str) -> None:
    assert make(f"{scheme}://u:p@host:5432/db").database_url == "postgresql+psycopg://u:p@host:5432/db"


def test_explicit_driver_is_left_alone() -> None:
    url = "postgresql+psycopg://u:p@host:5432/db"
    assert make(url).database_url == url


def test_cors_origins_are_split_and_trimmed() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://u:p@h/db",
        redis_url=REDIS,
        cors_origins=" https://a.example , ,http://localhost:3000,",
    )
    assert settings.cors_origin_list == ["https://a.example", "http://localhost:3000"]


def test_cors_origins_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS")  # the root conftest sets it for the app tests
    assert make("postgresql://u:p@h/db").cors_origin_list == []


def test_the_api_can_start_without_provider_keys_but_the_worker_cannot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API never calls Gnani or Groq, so it must not be forced to hold their keys."""
    monkeypatch.delenv("GNANI_API_KEY")
    monkeypatch.delenv("LLM_API_KEY")
    settings = make("postgresql://u:p@h/db")  # does not raise
    assert settings.missing_worker_secrets() == ["GNANI_API_KEY", "LLM_API_KEY"]


def test_a_blank_provider_key_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GNANI_API_KEY", "   ")  # what an untouched `GNANI_API_KEY=` line in .env.example gives
    assert make("postgresql://u:p@h/db").missing_worker_secrets() == ["GNANI_API_KEY"]


def test_a_configured_worker_reports_nothing_missing() -> None:
    assert make("postgresql://u:p@h/db").missing_worker_secrets() == []  # the root conftest sets both keys


def test_require_secret_names_the_variable_but_never_a_value() -> None:
    with pytest.raises(RuntimeError, match="GNANI_API_KEY is not set"):
        require_secret(None, "GNANI_API_KEY")
    with pytest.raises(RuntimeError, match="LLM_API_KEY is not set"):
        require_secret(SecretStr(""), "LLM_API_KEY")
    settings = make("postgresql://u:p@h/db")
    assert require_secret(settings.llm_api_key, "LLM_API_KEY") == "test-llm-key"


ROOT = Path(__file__).resolve().parents[2]


def import_in_fresh_process(module: str) -> subprocess.CompletedProcess[str]:
    """Import `module` in a new interpreter whose provider keys are blank (an env var beats the repo's .env file)."""
    env = {**os.environ, "GNANI_API_KEY": "", "LLM_API_KEY": "", "PYTHONPATH": str(ROOT / "backend")}  # as pytest sets
    return subprocess.run(
        [sys.executable, "-c", f"import {module}"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60
    )


def test_the_api_imports_and_starts_without_the_provider_keys() -> None:
    result = import_in_fresh_process("app.main")
    assert result.returncode == 0, result.stderr[-500:]


def test_the_worker_refuses_to_start_without_the_provider_keys_and_says_which() -> None:
    result = import_in_fresh_process("worker.tasks")
    assert result.returncode != 0
    assert "GNANI_API_KEY, LLM_API_KEY not set" in result.stderr
