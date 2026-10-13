import asyncio
import os
import weakref
from functools import lru_cache
from typing import Any, Optional

from redis import Redis


@lru_cache
def get_redis_client() -> Redis:
    """Return a shared Redis client singleton (string responses)."""
    return Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        decode_responses=True,
    )


@lru_cache
def get_redis_binary_client() -> Redis:
    """Return a shared Redis client for binary data (bytes responses).

    Use this for caching binary blobs like rendered tile images where
    ``decode_responses`` must be False.
    """
    return Redis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        decode_responses=False,
    )


# ---------------------------------------------------------------------------
# Async Redis client (one per event loop)
# ---------------------------------------------------------------------------

# An asyncio client's connections belong to the event loop that opened them; used or closed from another loop
# they fail ("Event loop is closed", "attached to a different loop"). The app has one loop per worker, but test
# clients, background threads with their own loop and pytest's loops do not share one, so each loop gets its own.
_async_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any]" = weakref.WeakKeyDictionary()


def get_async_redis():
    """Return the ``redis.asyncio.Redis`` client of the running event loop, created on first use.

    Binary mode (no ``decode_responses``) so raw bytes (MVT tiles, PNG tiles) are stored without encoding overhead.
    Returns None if Redis is unavailable or not configured, or when called with no event loop running.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    client = _async_clients.get(loop)
    if client is None:
        try:
            from redis.asyncio import Redis as AsyncRedis

            client = AsyncRedis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", 6379)),
                decode_responses=False,
            )
        except Exception:
            return None
        _async_clients[loop] = client
    return client


async def get_async_redis_for_ping():
    """Return a short-lived async Redis client for health checks (string mode).

    Caller is responsible for calling ``aclose()`` after use.
    """
    from redis.asyncio import Redis as AsyncRedis

    return AsyncRedis(
        host=os.environ.get("REDIS_HOST", "localhost"),
        port=int(os.environ.get("REDIS_PORT", 6379)),
        decode_responses=True,
    )


async def close_async_redis() -> None:
    """Close the running loop's async Redis client (call during shutdown) and forget clients whose loop is closed:
    their connections cannot be closed from here, and the loop that owned them is gone."""
    loop = asyncio.get_running_loop()
    for other in [other for other in list(_async_clients) if other is not loop and other.is_closed()]:
        _async_clients.pop(other, None)
    client = _async_clients.pop(loop, None)
    if client is not None:
        await client.aclose()
