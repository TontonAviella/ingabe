import asyncio
import logging
from pathlib import Path
from alembic import command
from alembic.config import Config

logger = logging.getLogger(__name__)


def _run_upgrade():
    """Synchronous Alembic upgrade — runs in a thread to avoid blocking the event loop."""
    project_root = Path(__file__).parent.parent.parent
    alembic_cfg = Config(project_root / "alembic.ini")
    alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))
    command.upgrade(alembic_cfg, "head")


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
