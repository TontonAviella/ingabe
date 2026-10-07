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

"""Integration tests for STAC satellite imagery service.

Tests cover:
- STACService instantiation and configuration
- Imagery search with various parameters
- Error handling for HTTP and connection failures
- NDVI asset extraction and filtering
- Singleton service retrieval
- REST API endpoint integration
"""

import pytest
from unittest.mock import patch, MagicMock
import requests

from src.services.stac_service import (
    STACService,
    get_stac_service,
    STAC_CATALOGS,
    RWANDA_BBOX,
    SENTINEL2_COLLECTIONS,
)


class TestSTACServiceInstantiation:
    """Test STACService initialization and configuration."""

    def test_default_catalog(self):
        """Verify default catalog is earth_search with correct URL."""
        service = STACService()
        assert service.catalog_name == "earth_search"
        assert service.catalog_url == STAC_CATALOGS["earth_search"]
        assert service.catalog_url == "https://earth-search.aws.element84.com/v1"

    def test_invalid_catalog_raises_error(self):
        """Verify invalid catalog name raises KeyError."""
        with pytest.raises(KeyError):
            STACService(catalog_name="invalid_catalog")



class TestSearchImagery:
    """Test imagery search functionality via HTTP fallback path."""

    @patch("src.services.stac_service._PYSTAC_CLIENT_AVAILABLE", False)
    @patch("src.services.stac_service.requests.Session.post")
    def test_search_imagery_default_parameters(self, mock_post):
        """Test search with default parameters returns correct structure."""
        # Mock STAC API response
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "type": "FeatureCollection",
            "features": [
                {
                    "id": "S2A_MSIL2A_20240115T081211_R121_T36MYE_20240115T121501",
                    "bbox": [28.9, -2.5, 29.1, -2.3],
                    "properties": {
                        "datetime": "2024-01-15T08:12:11Z",
                        "eo:cloud_cover": 5.2,
                        "platform": "sentinel-2a",
                    },
                    "assets": {
                        "B04": {"href": "https://example.com/b04.tif", "type": "image/tiff"},
                        "B08": {"href": "https://example.com/b08.tif", "type": "image/tiff"},
                        "visual": {"href": "https://example.com/visual.tif", "type": "image/tiff"},
                    },
                },
                {
                    "id": "S2A_MSIL2A_20240120T081211_R121_T36MYE_20240120T121501",
                    "bbox": [28.9, -2.5, 29.1, -2.3],
                    "properties": {
                        "datetime": "2024-01-20T08:12:11Z",
                        "eo:cloud_cover": 10.5,
                        "platform": "sentinel-2a",
                    },
                    "assets": {
                        "B04": {"href": "https://example.com/b04_2.tif", "type": "image/tiff"},
                        "B08": {"href": "https://example.com/b08_2.tif", "type": "image/tiff"},
                        "visual": {"href": "https://example.com/visual_2.tif", "type": "image/tiff"},
                    },
                },
            ],
        }
        mock_post.return_value = mock_response

        service = STACService()
        result = service.search_imagery()

        # Verify response structure
        assert "catalog" in result
        assert result["catalog"] == "earth_search"
        assert "collections" in result
        assert result["collections"] == [SENTINEL2_COLLECTIONS["earth_search"]]
        assert "bbox" in result
        assert result["bbox"] == RWANDA_BBOX
        assert "matched" in result
        assert result["matched"] == 2
        assert "items" in result
        assert len(result["items"]) == 2

        # Verify item structure
        item = result["items"][0]
        assert "id" in item
        assert "datetime" in item
        assert "cloud_cover" in item
        assert "platform" in item
        assert "bbox" in item
        assert "assets" in item
        assert "B04" in item["assets"]
        assert "B08" in item["assets"]
        assert "visual" in item["assets"]

    @patch("src.services.stac_service._PYSTAC_CLIENT_AVAILABLE", False)
    @patch("src.services.stac_service.requests.Session.post")
    def test_search_imagery_custom_bbox_and_datetime(self, mock_post):
        """Test search with custom bbox and datetime sends correct payload."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "type": "FeatureCollection",
            "features": [],
        }
        mock_post.return_value = mock_response

        service = STACService()
        custom_bbox = [29.0, -2.0, 30.0, -1.0]
        custom_datetime = "2024-01-01/2024-01-31"

        service.search_imagery(bbox=custom_bbox, datetime_range=custom_datetime)

        # Verify POST was called with correct payload
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        payload = call_args[1]["json"]

        assert payload["bbox"] == custom_bbox
        # Earth Search rejects bare dates (400): the range goes out in RFC 3339.
        assert payload["datetime"] == "2024-01-01T00:00:00Z/2024-01-31T23:59:59Z"
        assert payload["collections"] == [SENTINEL2_COLLECTIONS["earth_search"]]

    @patch("src.services.stac_service._PYSTAC_CLIENT_AVAILABLE", False)
    @patch("src.services.stac_service.requests.Session.post")
    def test_search_imagery_handles_http_error(self, mock_post):
        """Test search handles HTTP errors and returns error dict."""
        mock_post.side_effect = requests.HTTPError("502 Bad Gateway")

        service = STACService()
        result = service.search_imagery()

        # Verify error dict structure
        assert "error" in result
        assert "catalog" in result
        assert result["catalog"] == "earth_search"
        assert "502 Bad Gateway" in result["error"]

    @patch("src.services.stac_service._PYSTAC_CLIENT_AVAILABLE", False)
    @patch("src.services.stac_service.requests.Session.post")
    def test_search_imagery_handles_connection_error(self, mock_post):
        """Test search handles connection errors and returns error dict."""
        mock_post.side_effect = requests.ConnectionError("Failed to connect")

        service = STACService()
        result = service.search_imagery()

        # Verify error dict structure
        assert "error" in result
        assert "catalog" in result
        assert result["catalog"] == "earth_search"
        assert "Failed to connect" in result["error"]


class TestGetSTACServiceSingleton:
    """Test singleton service retrieval."""

    def test_get_stac_service_returns_same_instance(self):
        """Verify same instance returned for same catalog."""
        service1 = get_stac_service("earth_search")
        service2 = get_stac_service("earth_search")
        assert service1 is service2

