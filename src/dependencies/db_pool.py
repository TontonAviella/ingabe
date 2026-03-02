import asyncio
import logging
import ssl
import time
import asyncpg
from typing import Dict, AsyncGenerator, Tuple
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# Maximum number of connection pools to keep alive.
# Standard plan (2GB) can handle more concurrent PostGIS connections.
MAX_POOLS = 10

# Store pools by connection URI, with last-access timestamp
_connection_pools: Dict[str, Tuple[asyncpg.Pool, float]] = {}

# Lock to prevent concurrent pool creation
_pool_lock = asyncio.Lock()


async def _evict_oldest_pool() -> None:
    """Close and remove the least-recently-used pool when at capacity."""
    if not _connection_pools:
        return
    oldest_uri = min(_connection_pools, key=lambda k: _connection_pools[k][1])
    pool, _ = _connection_pools.pop(oldest_uri)
    try:
        await pool.close()
        logger.info("Evicted connection pool for capacity management")
    except Exception:
        logger.warning("Failed to close evicted pool cleanly")


async def get_or_create_pool(connection_uri: str) -> asyncpg.Pool:
    """Get existing pool or create new one for the connection URI.

    Enforces MAX_POOLS cap by evicting the least-recently-used pool.
    Thread-safe: uses lock to prevent duplicate pool creation.
    """
    async with _pool_lock:
        if connection_uri in _connection_pools:
            pool, _ = _connection_pools[connection_uri]
            _connection_pools[connection_uri] = (pool, time.monotonic())
            return pool

        # Evict oldest if at capacity
        if len(_connection_pools) >= MAX_POOLS:
            await _evict_oldest_pool()

        # Respect sslmode=disable in URI (e.g. internal Docker connections)
        parsed_uri = urlparse(connection_uri)
        qs = parse_qs(parsed_uri.query)
        ssl_disabled = qs.get("sslmode", [None])[0] == "disable"

        if ssl_disabled:
            ssl_param = False
        else:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            ssl_param = ssl_context

        pool = await asyncpg.create_pool(
            connection_uri, ssl=ssl_param, min_size=1, max_size=8, command_timeout=60
        )
        _connection_pools[connection_uri] = (pool, time.monotonic())
        return pool


async def remove_pool(connection_uri: str) -> None:
    """Explicitly close and remove a pool (e.g. when a PostGIS layer is deleted)."""
    async with _pool_lock:
        entry = _connection_pools.pop(connection_uri, None)
        if entry:
            pool, _ = entry
            try:
                await pool.close()
            except Exception:
                logger.warning("Failed to close removed pool cleanly")


async def close_all_pools() -> None:
    """Close all connection pools. Call during application shutdown."""
    for uri in list(_connection_pools.keys()):
        await remove_pool(uri)


@asynccontextmanager
async def get_pooled_connection(
    connection_uri: str,
) -> AsyncGenerator[asyncpg.Connection, None]:
    """Context manager that yields a database connection from pool"""
    pool = await get_or_create_pool(connection_uri)
    async with pool.acquire() as connection:
        yield connection
