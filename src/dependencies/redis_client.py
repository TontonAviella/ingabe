import os
from functools import lru_cache
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
