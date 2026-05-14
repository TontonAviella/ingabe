import asyncio
from logging.config import fileConfig

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
#
# disable_existing_loggers=False is critical: fileConfig defaults to True,
# which would disable every logger created before run_migrations() fires —
# notably mundi.cron.sage_alerts, mundi.senders.telegram, mundi.senders.whatsapp
# whose caplog-asserting tests then see empty caplog.records under combined
# pytest invocations (any test that calls run_migrations() before them).
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# add your model's MetaData object here
# for 'autogenerate' support
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.database.models import Base  # noqa: E402
from src.database.connection import DATABASE_URL  # noqa: E402

target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.

# Override the sqlalchemy.url with our DATABASE_URL
config.set_main_option("sqlalchemy.url", DATABASE_URL)


def _is_event_loop_running() -> bool:
    """Check if we're inside a running asyncio event loop (e.g. uvicorn)."""
    try:
        loop = asyncio.get_running_loop()
        return loop.is_running()
    except RuntimeError:
        return False


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_sync() -> None:
    """Run migrations using a synchronous engine.

    Used when called from within an already-running event loop
    (e.g. uvicorn lifespan) where asyncio.run() would fail.
    The async DATABASE_URL (postgresql+asyncpg://) is converted
    to its synchronous equivalent (postgresql+psycopg2://).
    """
    sync_url = DATABASE_URL.replace("+asyncpg", "+psycopg2")
    connectable = create_engine(sync_url, poolclass=pool.NullPool)

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


async def run_async_migrations() -> None:
    """Run migrations using an async engine.

    Used from the CLI (alembic upgrade head) where no event loop
    is running yet.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    Always use the synchronous engine — this is safe whether called
    from the CLI or from uvicorn (via asyncio.to_thread in migrate.py).
    """
    run_migrations_sync()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
