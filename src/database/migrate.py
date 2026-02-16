import asyncio
import logging
import redis.asyncio as redis
from pathlib import Path
from alembic import command
from alembic.config import Config
from concurrent.futures import ThreadPoolExecutor
import os

logger = logging.getLogger(__name__)


async def run_migrations():
    """Run Alembic migrations programmatically with Redis lock"""
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_client = redis.Redis(host=redis_host, port=6379)

    async with redis_client.lock("migration_lock", timeout=60, blocking_timeout=30):
        # Get the project root directory (mundi-public)
        project_root = Path(__file__).parent.parent.parent
        alembic_cfg = Config(project_root / "alembic.ini")

        # Set the script location to absolute path
        alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))

        def run_upgrade():
            """Run the synchronous alembic upgrade in a thread"""
            command.upgrade(alembic_cfg, "head")

        try:
            # Run synchronous Alembic command in a thread pool with timeout
            loop = asyncio.get_event_loop()
            with ThreadPoolExecutor() as executor:
                await asyncio.wait_for(
                    loop.run_in_executor(executor, run_upgrade), timeout=30.0
                )
            logger.info("Database migrations completed successfully")
            return True
        except asyncio.TimeoutError:
            logger.error("Migration failed: Timeout after 30 seconds")
            raise Exception("Migration timeout")
        except Exception as e:
            logger.error("Migration failed: %s", e)
            raise


# For running standalone
if __name__ == "__main__":
    asyncio.run(run_migrations())
