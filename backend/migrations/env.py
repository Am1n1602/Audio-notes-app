from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.core.config import get_settings
from app.db import models  # noqa: F401  (importing registers the tables on Base.metadata for autogenerate)
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers=False: the default silences every logger that already exists, which would also
    # mute the application's own logging when migrations run inside another process (the test suite).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata
# One source of truth for the connection string: DATABASE_URL via app settings, never a value in alembic.ini.
# Tests may point Alembic at a scratch database with config.set_main_option("sqlalchemy.url", ...).
database_url = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting (`alembic upgrade head --sql`)."""
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool: a migration is a one-shot process, no need to keep connections around.
    # connect_timeout: fail loudly if the database is unreachable instead of hanging the deploy step.
    engine = create_engine(database_url, poolclass=pool.NullPool, connect_args={"connect_timeout": 10})
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
