import os
from functools import lru_cache
from typing import Optional

from redis import Redis


@lru_cache
def get_redis_client() -> Redis:
    """Return a shared Redis client singleton (string responses)."""
    return Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        decode_responses=True,
    )


@lru_cache
def get_redis_binary_client() -> Redis:
    """Return a shared Redis client for binary data (bytes responses).

    Use this for caching binary blobs like rendered tile images where
    ``decode_responses`` must be False.
    """
    return Redis(
        host=os.environ["REDIS_HOST"],
        port=int(os.environ["REDIS_PORT"]),
        decode_responses=False,
    )


# ---------------------------------------------------------------------------
# Async Redis client (lazy singleton)
# ---------------------------------------------------------------------------

_async_redis = None


def get_async_redis():
    """Return a lazily-initialised ``redis.asyncio.Redis`` client.

    Binary mode (no ``decode_responses``) so raw bytes (MVT tiles, PNG tiles)
    are stored without encoding overhead.  The client is created once and reused.

    Returns None if Redis is unavailable or not configured.
    """
    global _async_redis
    if _async_redis is None:
        try:
            from redis.asyncio import Redis as AsyncRedis

            _async_redis = AsyncRedis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", 6379)),
                decode_responses=False,
            )
        except Exception:
            return None
    return _async_redis


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
    """Close the shared async Redis client. Call during shutdown."""
    global _async_redis
    if _async_redis is not None:
        await _async_redis.aclose()
        _async_redis = None
