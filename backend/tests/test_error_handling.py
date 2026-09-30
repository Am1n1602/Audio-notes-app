from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from helpers import HEADERS_A
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.main import app


@pytest.fixture
def client() -> Iterator[TestClient]:
    # raise_server_exceptions=False: behave like a real server and return the 500 response instead of re-raising
    yield TestClient(app, raise_server_exceptions=False, headers=HEADERS_A)
    app.dependency_overrides.clear()


def test_database_outage_is_a_structured_503_that_leaks_nothing(client: TestClient) -> None:
    def down() -> Iterator[Session]:
        raise OperationalError("SELECT 1", {}, Exception("password authentication failed for user audio"))
        yield  # pragma: no cover  (makes this a generator like the real dependency)

    app.dependency_overrides[get_db] = down
    res = client.get("/api/uploads")
    assert res.status_code == 503
    assert res.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "password" not in res.text and "audio" not in res.text  # no connection details for the client


def test_unexpected_errors_use_the_standard_shape_and_hide_internals(client: TestClient) -> None:
    def broken() -> Iterator[Session]:
        raise RuntimeError("internal detail: /srv/secret/path")
        yield  # pragma: no cover

    app.dependency_overrides[get_db] = broken
    res = client.get("/api/uploads")
    assert res.status_code == 500
    assert res.json() == {
        "error": {"code": "INTERNAL_ERROR", "message": "Something went wrong on our side. Please try again."}
    }
    assert "secret" not in res.text
