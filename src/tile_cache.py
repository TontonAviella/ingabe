"""Async Redis-backed raster tile cache.

Stores rendered PNG tiles keyed by ``tile:{layer_id}:{z}:{x}:{y}`` with a
configurable TTL (default 1 hour).  Uses ``redis.asyncio`` so that cache
operations do not block the event loop.

The browser/CDN ``Cache-Control: public, max-age=3600`` header on tile
responses acts as the first-level cache (closest to the client).  This
Redis layer is the *server-side* cache that avoids re-rendering tiles from
the COG on repeated requests.

Usage::

    from src.tile_cache import tile_cache

    # In the tile endpoint (async):
    cached = await tile_cache.get(layer_id, z, x, y)
    if cached is not None:
        return Response(content=cached, media_type="image/png", ...)

    # After rendering:
    await tile_cache.put(layer_id, z, x, y, png_bytes)

    # On layer data change (COG regeneration, re-upload):
    await tile_cache.invalidate_layer(layer_id)
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis.exceptions

logger = logging.getLogger(__name__)

# Default TTL in seconds — can be overridden via environment variable.
_DEFAULT_TTL = 3600  # 1 hour


def _ttl() -> int:
    return int(os.environ.get("TILE_CACHE_TTL", _DEFAULT_TTL))


def _enabled() -> bool:
    """Check if tile caching is enabled (default: True)."""
    return os.environ.get("TILE_CACHE_ENABLED", "true").lower() in ("true", "1", "yes")


# ---------------------------------------------------------------------------
# Async Redis client (lazy singleton)
# ---------------------------------------------------------------------------

_async_redis = None


def _get_async_redis():
    """Return a lazily-initialised ``redis.asyncio.Redis`` client.

    Binary mode (no ``decode_responses``) so raw PNG bytes are stored
    without encoding overhead.  The client is created once and reused.
    """
    global _async_redis
    if _async_redis is None:
        from redis.asyncio import Redis as AsyncRedis

        _async_redis = AsyncRedis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=False,
        )
    return _async_redis


class TileCache:
    """Async wrapper around ``redis.asyncio`` for tile caching.

    All public methods are coroutines so they integrate cleanly with the
    FastAPI async endpoints and never block the event loop.
    """

    # ------------------------------------------------------------------ #
    # Key helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _key(layer_id: str, z: int, x: int, y: int, fmt: str = "tile") -> str:
        return f"{fmt}:{layer_id}:{z}:{x}:{y}"

    @staticmethod
    def _pattern(layer_id: str, fmt: str = "tile") -> str:
        return f"{fmt}:{layer_id}:*"

    # ------------------------------------------------------------------ #
    # Core API (all async)
    # ------------------------------------------------------------------ #

    async def get(self, layer_id: str, z: int, x: int, y: int, fmt: str = "tile") -> Optional[bytes]:
        """Return cached tile bytes, or ``None`` on miss / error."""
        if not _enabled():
            return None
        try:
            client = _get_async_redis()
            data = await client.get(self._key(layer_id, z, x, y, fmt))
            if data is not None:
                logger.debug("tile cache HIT %s/%s/%s/%s [%s]", layer_id, z, x, y, fmt)
            return data
        except redis.exceptions.RedisError as e:
            logger.warning("Redis error in tile cache get: %s", e)
            return None
        except Exception as e:
            logger.error("Unexpected error in tile cache get: %s", e, exc_info=True)
            return None

    async def put(
        self,
        layer_id: str,
        z: int,
        x: int,
        y: int,
        png_bytes: bytes,
        ttl: Optional[int] = None,
        fmt: str = "tile",
    ) -> None:
        """Store rendered tile bytes in cache."""
        if not _enabled():
            return
        try:
            client = _get_async_redis()
            await client.setex(
                self._key(layer_id, z, x, y, fmt),
                ttl or _ttl(),
                png_bytes,
            )
        except redis.exceptions.RedisError as e:
            logger.warning("Redis error in tile cache put: %s", e)
        except Exception as e:
            logger.error("Unexpected error in tile cache put: %s", e, exc_info=True)

    async def invalidate_layer(self, layer_id: str) -> int:
        """Delete all cached tiles (raster + MVT) for *layer_id*.

        Uses ``SCAN`` + ``DELETE`` to avoid blocking Redis with a single
        large ``KEYS`` call.

        Returns the number of keys deleted.
        """
        if not _enabled():
            return 0
        try:
            client = _get_async_redis()
            deleted = 0
            # Clear both raster ("tile:") and vector ("mvt:") caches
            for fmt in ("tile", "mvt"):
                pattern = self._pattern(layer_id, fmt)
                cursor: int = 0
                while True:
                    cursor, keys = await client.scan(cursor=cursor, match=pattern, count=200)
                    if keys:
                        deleted += await client.delete(*keys)
                    if cursor == 0:
                        break
            if deleted:
                logger.info(
                    "tile cache: invalidated %d tiles for layer %s",
                    deleted,
                    layer_id,
                )
            return deleted
        except redis.exceptions.RedisError as e:
            logger.warning("Redis error in tile cache invalidate: %s", e)
            return 0
        except Exception as e:
            logger.error("Unexpected error in tile cache invalidate: %s", e, exc_info=True)
            return 0

    async def invalidate_tile(self, layer_id: str, z: int, x: int, y: int) -> bool:
        """Delete a single cached tile. Returns True if the key existed."""
        if not _enabled():
            return False
        try:
            client = _get_async_redis()
            return bool(await client.delete(self._key(layer_id, z, x, y)))
        except redis.exceptions.RedisError as e:
            logger.warning("Redis error in tile cache invalidate_tile: %s", e)
            return False
        except Exception as e:
            logger.error("Unexpected error in tile cache invalidate_tile: %s", e, exc_info=True)
            return False


# Module-level singleton
tile_cache = TileCache()
