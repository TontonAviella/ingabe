import asyncio
import logging
from pathlib import Path
from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)

# Arbitrary constant used as PostgreSQL advisory lock ID to serialise migrations.
_MIGRATION_LOCK_ID = 2908697512


def _run_upgrade():
    """Synchronous Alembic upgrade with advisory lock for concurrent-safety.

    Multiple processes (e.g. pytest-xdist workers or horizontally scaled
    app instances) may call this simultaneously.  A PostgreSQL advisory
    lock ensures only one actually runs the migration at a time; the
    others block until the lock is released, then run ``upgrade head``
    as a harmless no-op.
    """
    import psycopg2
    from src.database.pool import _build_postgres_url

    project_root = Path(__file__).parent.parent.parent
    alembic_cfg = Config(project_root / "alembic.ini")
    alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))

    conn = psycopg2.connect(_build_postgres_url())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_ID,))
        try:
            command.upgrade(alembic_cfg, "head")
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_ID,))
    finally:
        conn.close()


async def run_migrations():
    """Run Alembic migrations programmatically.

    Runs synchronously since asyncio.to_thread() causes deadlocks
    with uvicorn's lifespan context manager.
    """
    try:
        _run_upgrade()  # Run synchronously - blocking is acceptable during startup
        logger.info("Database migrations completed successfully")
        return True
    except Exception as e:
        logger.error("Migration failed: %s", e)
        raise


# For running standalone
if __name__ == "__main__":
    asyncio.run(run_migrations())
