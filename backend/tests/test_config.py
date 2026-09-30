import pytest

from app.core.config import Settings

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
