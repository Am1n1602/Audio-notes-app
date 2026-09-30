from alembic import command
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
