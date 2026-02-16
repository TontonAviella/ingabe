"""Unit tests for src.tile_cache.TileCache.

These tests mock the async Redis client so they run without a Redis server.
All TileCache methods are async, so tests use pytest-asyncio.
"""

import asyncio
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tile_cache import TileCache


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_redis():
    """Return an AsyncMock Redis client and patch it into tile_cache."""
    client = AsyncMock()
    with patch("src.tile_cache._get_async_redis", return_value=client):
        yield client


@pytest.fixture
def cache():
    return TileCache()


@pytest.fixture
def sample_png() -> bytes:
    """A minimal valid PNG (1x1 transparent pixel)."""
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Key generation (sync — no Redis needed)
# ---------------------------------------------------------------------------

class TestKeyGeneration:
    def test_key_format(self, cache):
        assert cache._key("L123", 5, 10, 20) == "tile:L123:5:10:20"

    def test_pattern_format(self, cache):
        assert cache._pattern("L123") == "tile:L123:*"


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------

class TestGet:
    @pytest.mark.asyncio
    async def test_cache_hit(self, cache, mock_redis, sample_png):
        mock_redis.get.return_value = sample_png
        result = await cache.get("L1", 5, 10, 20)
        assert result == sample_png
        mock_redis.get.assert_awaited_once_with("tile:L1:5:10:20")

    @pytest.mark.asyncio
    async def test_cache_miss(self, cache, mock_redis):
        mock_redis.get.return_value = None
        result = await cache.get("L1", 5, 10, 20)
        assert result is None

    @pytest.mark.asyncio
    async def test_redis_error_returns_none(self, cache, mock_redis):
        mock_redis.get.side_effect = ConnectionError("Redis down")
        result = await cache.get("L1", 5, 10, 20)
        assert result is None

    @pytest.mark.asyncio
    async def test_disabled_returns_none(self, cache, mock_redis, sample_png):
        mock_redis.get.return_value = sample_png
        with patch.dict("os.environ", {"TILE_CACHE_ENABLED": "false"}):
            result = await cache.get("L1", 5, 10, 20)
        assert result is None
        mock_redis.get.assert_not_awaited()


# ---------------------------------------------------------------------------
# Put
# ---------------------------------------------------------------------------

class TestPut:
    @pytest.mark.asyncio
    async def test_stores_with_default_ttl(self, cache, mock_redis, sample_png):
        await cache.put("L1", 5, 10, 20, sample_png)
        mock_redis.setex.assert_awaited_once_with(
            "tile:L1:5:10:20", 3600, sample_png
        )

    @pytest.mark.asyncio
    async def test_custom_ttl(self, cache, mock_redis, sample_png):
        await cache.put("L1", 5, 10, 20, sample_png, ttl=120)
        mock_redis.setex.assert_awaited_once_with(
            "tile:L1:5:10:20", 120, sample_png
        )

    @pytest.mark.asyncio
    async def test_env_ttl_override(self, cache, mock_redis, sample_png):
        with patch.dict("os.environ", {"TILE_CACHE_TTL": "7200"}):
            await cache.put("L1", 5, 10, 20, sample_png)
        mock_redis.setex.assert_awaited_once_with(
            "tile:L1:5:10:20", 7200, sample_png
        )

    @pytest.mark.asyncio
    async def test_redis_error_is_swallowed(self, cache, mock_redis, sample_png):
        mock_redis.setex.side_effect = ConnectionError("Redis down")
        # Should not raise
        await cache.put("L1", 5, 10, 20, sample_png)

    @pytest.mark.asyncio
    async def test_disabled_skips_write(self, cache, mock_redis, sample_png):
        with patch.dict("os.environ", {"TILE_CACHE_ENABLED": "false"}):
            await cache.put("L1", 5, 10, 20, sample_png)
        mock_redis.setex.assert_not_awaited()


# ---------------------------------------------------------------------------
# Invalidate layer
# ---------------------------------------------------------------------------

class TestInvalidateLayer:
    @pytest.mark.asyncio
    async def test_deletes_matching_keys(self, cache, mock_redis):
        keys = [b"tile:L1:5:10:20", b"tile:L1:6:20:40"]
        # scan is called twice: once for "tile:" prefix, once for "mvt:" prefix
        mock_redis.scan.side_effect = [
            (0, keys),   # tile:L1:* → 2 keys
            (0, []),     # mvt:L1:* → 0 keys
        ]
        mock_redis.delete.return_value = 2

        deleted = await cache.invalidate_layer("L1")
        assert deleted == 2
        assert mock_redis.scan.await_count == 2
        mock_redis.delete.assert_awaited_once_with(*keys)

    @pytest.mark.asyncio
    async def test_no_matching_keys(self, cache, mock_redis):
        # Both tile: and mvt: scans return nothing
        mock_redis.scan.side_effect = [(0, []), (0, [])]
        deleted = await cache.invalidate_layer("L1")
        assert deleted == 0
        mock_redis.delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_multi_batch_scan(self, cache, mock_redis):
        """Simulates SCAN needing two iterations for the tile prefix."""
        batch1 = [b"tile:L1:5:10:20"]
        batch2 = [b"tile:L1:6:20:40"]
        mock_redis.scan.side_effect = [
            (42, batch1),  # tile: first call: cursor 42, 1 key
            (0, batch2),   # tile: second call: cursor 0, 1 key
            (0, []),       # mvt: scan returns nothing
        ]
        mock_redis.delete.return_value = 1

        deleted = await cache.invalidate_layer("L1")
        assert deleted == 2
        assert mock_redis.scan.await_count == 3  # 2 for tile + 1 for mvt
        assert mock_redis.delete.await_count == 2

    @pytest.mark.asyncio
    async def test_redis_error_returns_zero(self, cache, mock_redis):
        mock_redis.scan.side_effect = ConnectionError("Redis down")
        deleted = await cache.invalidate_layer("L1")
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_disabled_returns_zero(self, cache, mock_redis):
        with patch.dict("os.environ", {"TILE_CACHE_ENABLED": "false"}):
            deleted = await cache.invalidate_layer("L1")
        assert deleted == 0
        mock_redis.scan.assert_not_awaited()


# ---------------------------------------------------------------------------
# Invalidate single tile
# ---------------------------------------------------------------------------

class TestInvalidateTile:
    @pytest.mark.asyncio
    async def test_deletes_single_key(self, cache, mock_redis):
        mock_redis.delete.return_value = 1
        assert await cache.invalidate_tile("L1", 5, 10, 20) is True
        mock_redis.delete.assert_awaited_once_with("tile:L1:5:10:20")

    @pytest.mark.asyncio
    async def test_key_not_found(self, cache, mock_redis):
        mock_redis.delete.return_value = 0
        assert await cache.invalidate_tile("L1", 5, 10, 20) is False

    @pytest.mark.asyncio
    async def test_redis_error_returns_false(self, cache, mock_redis):
        mock_redis.delete.side_effect = ConnectionError("Redis down")
        assert await cache.invalidate_tile("L1", 5, 10, 20) is False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_module_singleton_exists(self):
        from src.tile_cache import tile_cache
        assert isinstance(tile_cache, TileCache)
