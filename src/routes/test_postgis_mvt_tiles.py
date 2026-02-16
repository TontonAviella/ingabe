# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unit tests for PostGIS MVT tile generation (fetch_mvt_tile).

Tests cover error handling, column validation, and cache behaviour
without requiring a live PostGIS database — all external dependencies
are mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from src.database.models import MapLayer
from src.postgis_tiles import fetch_mvt_tile, _validate_column_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_layer(**overrides) -> MapLayer:
    """Build a minimal MapLayer suitable for MVT tests."""
    defaults = {
        "layer_id": "LtestMVT001",
        "name": "test_layer",
        "type": "postgis",
        "postgis_connection_id": "Ctest001",
        "postgis_query": 'SELECT id, geom, name FROM "public"."parcels"',
        "postgis_attribute_column_list": ["name"],
    }
    defaults.update(overrides)
    layer = MagicMock(spec=MapLayer)
    for k, v in defaults.items():
        setattr(layer, k, v)
    return layer


# ---------------------------------------------------------------------------
# Column name validation
# ---------------------------------------------------------------------------


class TestColumnNameValidation:
    """Tests for _validate_column_name (SQL injection defence)."""

    def test_valid_identifier(self):
        assert _validate_column_name("population") == '"population"'

    def test_valid_identifier_with_underscore(self):
        assert _validate_column_name("crop_type") == '"crop_type"'

    def test_rejects_semicolon(self):
        with pytest.raises(ValueError, match="Invalid column name"):
            _validate_column_name("name; DROP TABLE--")

    def test_rejects_single_quote(self):
        with pytest.raises(ValueError, match="Invalid column name"):
            _validate_column_name("name'")

    def test_rejects_space(self):
        with pytest.raises(ValueError, match="Invalid column name"):
            _validate_column_name("col name")

    def test_rejects_leading_digit(self):
        with pytest.raises(ValueError, match="Invalid column name"):
            _validate_column_name("1col")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="Invalid column name"):
            _validate_column_name("")


# ---------------------------------------------------------------------------
# fetch_mvt_tile error paths
# ---------------------------------------------------------------------------


class TestFetchMvtTile:
    """Tests for fetch_mvt_tile error handling."""

    @pytest.fixture
    def mock_conn(self):
        """AsyncMock asyncpg connection with transaction support."""
        conn = AsyncMock(spec=asyncpg.Connection)
        # Make transaction() return an async context manager
        tx = AsyncMock()
        tx.__aenter__ = AsyncMock(return_value=tx)
        tx.__aexit__ = AsyncMock(return_value=False)
        conn.transaction.return_value = tx
        return conn

    @pytest.fixture(autouse=True)
    def _disable_redis(self):
        """Disable Redis so tests don't depend on a running server."""
        with patch("src.postgis_tiles._get_async_redis", return_value=None):
            yield

    async def test_non_postgis_layer_rejected(self, mock_conn):
        """Layers that aren't type=postgis should be rejected with 400."""
        layer = _make_layer(type="vector")
        with pytest.raises(Exception) as exc_info:
            await fetch_mvt_tile(layer, mock_conn, 10, 512, 512)
        assert exc_info.value.status_code == 400
        assert "not a PostGIS type" in exc_info.value.detail

    async def test_missing_attribute_columns_rejected(self, mock_conn):
        """Layers with no attribute columns should be rejected with 400."""
        layer = _make_layer(postgis_attribute_column_list=None)
        with pytest.raises(Exception) as exc_info:
            await fetch_mvt_tile(layer, mock_conn, 10, 512, 512)
        assert exc_info.value.status_code == 400
        assert "no attribute columns" in exc_info.value.detail

    async def test_unsafe_column_name_rejected(self, mock_conn):
        """Column names with SQL injection attempts should be rejected."""
        layer = _make_layer(
            postgis_attribute_column_list=["name", "val; DROP TABLE users--"]
        )
        with pytest.raises(Exception) as exc_info:
            await fetch_mvt_tile(layer, mock_conn, 10, 512, 512)
        assert exc_info.value.status_code == 400
        assert "unsafe column name" in exc_info.value.detail

    async def test_query_timeout_returns_504(self, mock_conn):
        """QueryCanceledError (statement_timeout) should map to 504."""
        mock_conn.fetchval = AsyncMock(side_effect=asyncpg.QueryCanceledError("statement timeout"))
        with pytest.raises(Exception) as exc_info:
            await fetch_mvt_tile(_make_layer(), mock_conn, 10, 512, 512)
        assert exc_info.value.status_code == 504
        assert "timed out" in exc_info.value.detail

    async def test_successful_fetch_returns_bytes(self, mock_conn):
        """Happy path: valid query returns MVT bytes."""
        fake_mvt = b"\x1a\x00"  # minimal protobuf-like bytes
        mock_conn.fetchval = AsyncMock(return_value=fake_mvt)
        result = await fetch_mvt_tile(_make_layer(), mock_conn, 10, 512, 512)
        assert result == fake_mvt

    async def test_empty_tile_returns_none(self, mock_conn):
        """When PostGIS returns None (no features in tile), result is None."""
        mock_conn.fetchval = AsyncMock(return_value=None)
        result = await fetch_mvt_tile(_make_layer(), mock_conn, 10, 512, 512)
        assert result is None

    async def test_redis_error_on_cache_write_is_non_fatal(self, mock_conn):
        """Redis write failure during caching should not break tile serving."""
        import redis.exceptions

        fake_mvt = b"\x1a\x00"
        mock_conn.fetchval = AsyncMock(return_value=fake_mvt)

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)  # cache miss
        mock_redis.setex = AsyncMock(
            side_effect=redis.exceptions.ConnectionError("redis down")
        )

        with patch("src.postgis_tiles._get_async_redis", return_value=mock_redis):
            result = await fetch_mvt_tile(_make_layer(), mock_conn, 10, 512, 512)
        # Should still return the tile despite Redis failure
        assert result == fake_mvt

    async def test_redis_cache_hit_returns_cached_tile(self, mock_conn):
        """When Redis has a cached tile, it should be returned without querying PostGIS."""
        cached_mvt = b"\x1a\x01cached"
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=cached_mvt)

        with patch("src.postgis_tiles._get_async_redis", return_value=mock_redis):
            result = await fetch_mvt_tile(_make_layer(), mock_conn, 10, 512, 512)
        assert result == cached_mvt
        # PostGIS should NOT have been queried
        mock_conn.fetchval.assert_not_awaited()
