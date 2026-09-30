from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fakes import FakeQueue, FakeStorage
from fastapi.testclient import TestClient
from helpers import assert_test_database
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import get_db, get_sessionmaker
from app.main import app
from app.providers.queue import get_queue
from app.providers.storage import get_storage

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def recreate_database(name: str) -> str:
    """(Re)create an empty database next to the configured one; returns its URL."""
    assert_test_database(name)
    base = make_url(get_settings().database_url)
    admin = create_engine(base.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        pytest.fail(
            f"Postgres is not reachable ({type(exc.orig).__name__}). Start it with: docker compose up -d --wait"
        )
    finally:
        admin.dispose()
    return base.set(database=name).render_as_string(hide_password=False)


@pytest.fixture(scope="session", autouse=False)
def migrated_database() -> None:
    """A fresh audio_notes_test database built by the REAL migrations, so tests exercise the real schema."""
    recreate_database(make_url(get_settings().database_url).database or "audio_notes_test")
    command.upgrade(alembic_config(), "head")


@pytest.fixture
def db(migrated_database: None) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        session.execute(text("TRUNCATE audio_jobs"))
        session.commit()
        yield session


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def queue() -> FakeQueue:
    return FakeQueue()


@pytest.fixture
def api(db: Session, storage: FakeStorage, queue: FakeQueue) -> Iterator[TestClient]:
    """The real app + real Postgres, with storage and the job queue replaced by in-memory fakes."""
    app.dependency_overrides[get_storage] = lambda: storage
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides.pop(get_db, None)
    yield TestClient(app)
    app.dependency_overrides.clear()
