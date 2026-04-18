# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""Tests for DE Africa WOfS + Cropland validation enrichment.

All tests use synthetic data and mocked STAC/rasterio calls. No network access.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

SAMPLE_BBOX: Tuple[float, float, float, float] = (29.3, -2.0, 29.4, -1.9)
SAMPLE_BBOX_OUTSIDE_AFRICA: Tuple[float, float, float, float] = (10.0, 48.0, 10.1, 48.1)


def _make_stac_item(
    collection: str,
    asset_key: str = "frequency",
    href: str = "s3://fake/cog.tif",
    datetime_str: str = "2024-06-15T00:00:00Z",
) -> Dict[str, Any]:
    """Create a minimal STAC item for testing."""
    return {
        "id": f"test-{collection}-item",
        "properties": {"datetime": datetime_str},
        "assets": {asset_key: {"href": href}},
    }


def _mock_read_window_factory(
    arr: np.ndarray,
) -> Any:
    """Return a function that mimics _read_window returning (arr, transform)."""
    def _mock(href: str, bbox: Tuple[float, float, float, float]) -> Optional[Tuple[np.ndarray, Any]]:
        return (arr, MagicMock())
    return _mock


# ---------------------------------------------------------------------------
# _round_bbox
# ---------------------------------------------------------------------------

class TestRoundBbox:
    def test_rounds_to_6dp(self):
        from src.services.deafrica_stac import _round_bbox
        bbox = (29.123456789, -2.000000001, 29.400000009, -1.899999991)
        result = _round_bbox(bbox)
        assert result == (29.123457, -2.0, 29.4, -1.9)

    def test_preserves_exact_values(self):
        from src.services.deafrica_stac import _round_bbox
        bbox = (29.3, -2.0, 29.4, -1.9)
        assert _round_bbox(bbox) == bbox


# ---------------------------------------------------------------------------
# _search_collection_items
# ---------------------------------------------------------------------------

class TestSearchCollectionItems:
    @patch("src.services.deafrica_stac.httpx.get")
    def test_returns_features(self, mock_get):
        from src.services.deafrica_stac import _search_collection_items
        item = _make_stac_item("wofs_ls")
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"features": [item]},
            raise_for_status=lambda: None,
        )
        result = _search_collection_items("wofs_ls", SAMPLE_BBOX)
        assert len(result) == 1
        assert result[0]["id"] == "test-wofs_ls-item"

    @patch("src.services.deafrica_stac.httpx.get")
    def test_returns_empty_on_timeout(self, mock_get):
        from src.services.deafrica_stac import _search_collection_items
        mock_get.side_effect = Exception("Connection timeout")
        result = _search_collection_items("wofs_ls", SAMPLE_BBOX)
        assert result == []

    @patch("src.services.deafrica_stac.httpx.get")
    def test_returns_empty_on_no_features(self, mock_get):
        from src.services.deafrica_stac import _search_collection_items
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"features": []},
            raise_for_status=lambda: None,
        )
        result = _search_collection_items("wofs_ls", SAMPLE_BBOX)
        assert result == []


# ---------------------------------------------------------------------------
# _cached_water_frequency
# ---------------------------------------------------------------------------

class TestCachedWaterFrequency:
    def setup_method(self):
        """Clear lru_cache between tests."""
        from src.services.deafrica_stac import _cached_water_frequency
        _cached_water_frequency.cache_clear()

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_mean_frequency(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_water_frequency
        # WOfS frequency: 50% water frequency across all pixels
        arr = np.full((10, 10), 0.5, dtype=np.float32)
        mock_search.return_value = [_make_stac_item("wofs_ls")]
        mock_read.return_value = (arr, MagicMock())
        result = _cached_water_frequency(SAMPLE_BBOX)
        assert result is not None
        freq, year = result
        assert freq == 0.5
        assert year == 2024

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_normalizes_percentage_values(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_water_frequency
        # WOfS stored as 0-100 percentage
        arr = np.full((10, 10), 75.0, dtype=np.float32)
        mock_search.return_value = [_make_stac_item("wofs_ls")]
        mock_read.return_value = (arr, MagicMock())
        result = _cached_water_frequency(SAMPLE_BBOX)
        assert result is not None
        assert result[0] == 0.75

    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_none_on_no_items(self, mock_search):
        from src.services.deafrica_stac import _cached_water_frequency
        mock_search.return_value = []
        assert _cached_water_frequency(SAMPLE_BBOX) is None

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_none_on_read_failure(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_water_frequency
        mock_search.return_value = [_make_stac_item("wofs_ls")]
        mock_read.return_value = None
        assert _cached_water_frequency(SAMPLE_BBOX) is None

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_none_on_all_nan(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_water_frequency
        arr = np.full((10, 10), np.nan, dtype=np.float32)
        mock_search.return_value = [_make_stac_item("wofs_ls")]
        mock_read.return_value = (arr, MagicMock())
        assert _cached_water_frequency(SAMPLE_BBOX) is None


# ---------------------------------------------------------------------------
# _cached_cropland
# ---------------------------------------------------------------------------

class TestCachedCropland:
    def setup_method(self):
        from src.services.deafrica_stac import _cached_cropland
        _cached_cropland.cache_clear()

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_cropland_fraction(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_cropland
        # 60 out of 100 pixels are cropland
        arr = np.zeros((10, 10), dtype=np.uint8)
        arr[:6, :] = 1
        mock_search.return_value = [_make_stac_item("crop_mask", asset_key="mask")]
        mock_read.return_value = (arr, MagicMock())
        result = _cached_cropland(SAMPLE_BBOX)
        assert result is not None
        assert result[0] == 0.6
        assert result[1] == 2024

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_all_cropland(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_cropland
        arr = np.ones((10, 10), dtype=np.uint8)
        mock_search.return_value = [_make_stac_item("crop_mask", asset_key="mask")]
        mock_read.return_value = (arr, MagicMock())
        result = _cached_cropland(SAMPLE_BBOX)
        assert result is not None
        assert result[0] == 1.0

    @patch("src.services.deafrica_stac._read_window")
    @patch("src.services.deafrica_stac._search_collection_items")
    def test_no_cropland(self, mock_search, mock_read):
        from src.services.deafrica_stac import _cached_cropland
        arr = np.zeros((10, 10), dtype=np.uint8)
        mock_search.return_value = [_make_stac_item("crop_mask", asset_key="mask")]
        mock_read.return_value = (arr, MagicMock())
        result = _cached_cropland(SAMPLE_BBOX)
        assert result is not None
        assert result[0] == 0.0

    @patch("src.services.deafrica_stac._search_collection_items")
    def test_returns_none_on_no_items(self, mock_search):
        from src.services.deafrica_stac import _cached_cropland
        mock_search.return_value = []
        assert _cached_cropland(SAMPLE_BBOX) is None


# ---------------------------------------------------------------------------
# enrich_with_validation
# ---------------------------------------------------------------------------

class TestEnrichWithValidation:
    def setup_method(self):
        from src.services.deafrica_stac import _cached_water_frequency, _cached_cropland
        _cached_water_frequency.cache_clear()
        _cached_cropland.cache_clear()

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_adds_both_keys(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = (0.35, 2024)
        mock_crop.return_value = (0.8, 2024)
        result = enrich_with_validation({"status": "success"}, SAMPLE_BBOX)
        assert result["wofs_mean_frequency"] == 0.35
        assert result["cropland_fraction"] == 0.8
        assert result["validation_source"] == "Digital Earth Africa (WOfS + Cropland Extent)"
        assert result["validation_data_year"] == 2024

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_wofs_only_on_cropland_failure(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = (0.1, 2023)
        mock_crop.return_value = None
        result = enrich_with_validation({"status": "success"}, SAMPLE_BBOX)
        assert result["wofs_mean_frequency"] == 0.1
        assert "cropland_fraction" not in result
        assert result["validation_data_year"] == 2023

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_cropland_only_on_wofs_failure(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = None
        mock_crop.return_value = (0.9, 2024)
        result = enrich_with_validation({"status": "success"}, SAMPLE_BBOX)
        assert "wofs_mean_frequency" not in result
        assert result["cropland_fraction"] == 0.9

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_no_keys_on_both_failure(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = None
        mock_crop.return_value = None
        result = enrich_with_validation({"status": "success"}, SAMPLE_BBOX)
        assert "wofs_mean_frequency" not in result
        assert "cropland_fraction" not in result
        assert "validation_source" not in result
        assert result["status"] == "success"  # original keys preserved

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_exception_in_wofs_still_tries_cropland(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.side_effect = Exception("STAC timeout")
        mock_crop.return_value = (0.7, 2024)
        result = enrich_with_validation({"status": "success"}, SAMPLE_BBOX)
        assert "wofs_mean_frequency" not in result
        assert result["cropland_fraction"] == 0.7

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_uses_max_data_year(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = (0.2, 2023)
        mock_crop.return_value = (0.6, 2024)
        result = enrich_with_validation({}, SAMPLE_BBOX)
        assert result["validation_data_year"] == 2024

    @patch("src.services.deafrica_stac._cached_cropland")
    @patch("src.services.deafrica_stac._cached_water_frequency")
    def test_bbox_rounded_before_lookup(self, mock_wofs, mock_crop):
        from src.services.deafrica_stac import enrich_with_validation
        mock_wofs.return_value = (0.1, 2024)
        mock_crop.return_value = (0.5, 2024)
        bbox = (29.3000001, -2.0000009, 29.4000002, -1.8999998)
        enrich_with_validation({}, bbox)
        # Check that the rounded bbox was passed to the cached functions
        called_bbox = mock_wofs.call_args[0][0]
        assert called_bbox == (29.3, -2.000001, 29.4, -1.9)


# ---------------------------------------------------------------------------
# _enrich_ndvi_with_cropland
# ---------------------------------------------------------------------------

class TestEnrichNdviWithCropland:
    def setup_method(self):
        from src.services.deafrica_stac import _cached_cropland
        _cached_cropland.cache_clear()

    @patch("src.services.deafrica_stac._cached_cropland")
    def test_adds_cropland_fraction(self, mock_crop):
        from src.services.sar_ndvi import _enrich_ndvi_with_cropland
        mock_crop.return_value = (0.85, 2024)
        result = _enrich_ndvi_with_cropland({"predicted_ndvi": 0.6}, SAMPLE_BBOX)
        assert result["cropland_fraction"] == 0.85
        assert "cropland_warning" not in result

    @patch("src.services.deafrica_stac._cached_cropland")
    def test_adds_warning_for_low_cropland(self, mock_crop):
        from src.services.sar_ndvi import _enrich_ndvi_with_cropland
        mock_crop.return_value = (0.12, 2024)
        result = _enrich_ndvi_with_cropland({"predicted_ndvi": 0.6}, SAMPLE_BBOX)
        assert result["cropland_fraction"] == 0.12
        assert "cropland_warning" in result
        assert "0.12" in result["cropland_warning"]

    @patch("src.services.deafrica_stac._cached_cropland")
    def test_no_warning_at_threshold(self, mock_crop):
        from src.services.sar_ndvi import _enrich_ndvi_with_cropland
        mock_crop.return_value = (0.3, 2024)
        result = _enrich_ndvi_with_cropland({"predicted_ndvi": 0.6}, SAMPLE_BBOX)
        assert result["cropland_fraction"] == 0.3
        assert "cropland_warning" not in result

    @patch("src.services.deafrica_stac._cached_cropland")
    def test_graceful_on_failure(self, mock_crop):
        from src.services.sar_ndvi import _enrich_ndvi_with_cropland
        mock_crop.side_effect = Exception("network error")
        result = _enrich_ndvi_with_cropland({"predicted_ndvi": 0.6}, SAMPLE_BBOX)
        assert "cropland_fraction" not in result
        assert result["predicted_ndvi"] == 0.6  # original preserved
