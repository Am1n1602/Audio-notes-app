from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app
from app.services import health as health_service


@pytest.fixture
def client() -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: None  # no real Postgres in unit tests
    yield TestClient(app)
    app.dependency_overrides.clear()


def stub_checks(monkeypatch: pytest.MonkeyPatch, *, db: bool, redis: bool) -> None:
    monkeypatch.setattr(health_service, "check_db", lambda _db: db)
    monkeypatch.setattr(health_service, "check_redis", lambda _url: redis)


def test_health_ok(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_checks(monkeypatch, db=True, redis=True)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "db": "ok", "redis": "ok"}


@pytest.mark.parametrize(
    ("db", "redis"),
    [(False, True), (True, False), (False, False)],
)
def test_health_reports_each_failing_dependency_with_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, db: bool, redis: bool
) -> None:
    stub_checks(monkeypatch, db=db, redis=redis)
    res = client.get("/api/health")
    assert res.status_code == 503
    assert res.json() == {
        "status": "degraded",
        "db": "ok" if db else "error",
        "redis": "ok" if redis else "error",
    }


def test_check_redis_is_false_when_unreachable() -> None:
    assert health_service.check_redis("redis://127.0.0.1:1/0") is False  # nothing listens on port 1


def test_check_db_is_false_when_unreachable() -> None:
    engine = create_engine("postgresql+psycopg://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1})
    with Session(engine) as session:
        assert health_service.check_db(session) is False


def test_cors_allows_configured_origin_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    stub_checks(monkeypatch, db=True, redis=True)
    allowed = client.get("/api/health", headers={"Origin": "http://localhost:3000"})
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3000"
    blocked = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in blocked.headers
