import pytest
from alembic import command
from helpers import assert_test_database
from sqlalchemy import create_engine, inspect

from conftest import alembic_config, recreate_database

SCRATCH_DB = "audio_notes_migration_test"


def tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()


def test_migrations_apply_reverse_and_match_the_models() -> None:
    url = recreate_database(SCRATCH_DB)
    cfg = alembic_config(url)

    command.upgrade(cfg, "head")
    assert tables(url) == {"audio_jobs"}

    command.check(cfg)  # raises if the models drifted from the migrations (someone forgot `alembic revision`)

    command.downgrade(cfg, "base")
    assert tables(url) == set()

    command.upgrade(cfg, "head")  # and it can be applied again
    assert tables(url) == {"audio_jobs"}


@pytest.mark.parametrize("name", ["audio_notes", "postgres", "production", "audio_notes_test_backup"])
def test_the_name_guard_refuses_anything_that_is_not_a_test_database(name: str) -> None:
    with pytest.raises(RuntimeError, match="must end in '_test'"):
        assert_test_database(name)  # a pure check: it cannot touch a database, even if it is broken


def test_recreate_database_checks_the_name_before_it_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    """recreate_database DROPs its target, so this test must never reach a real database: connecting is replaced by
    a failure. (A version of this test that passed a real database name once wiped the dev database when the guard
    was deliberately broken to check that the test notices.)"""
    import conftest

    def would_connect(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("connected to Postgres before checking the database name")

    monkeypatch.setattr(conftest, "create_engine", would_connect)
    with pytest.raises(RuntimeError, match="must end in '_test'"):
        recreate_database("audio_notes")
