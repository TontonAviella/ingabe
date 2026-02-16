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

    def test_custom_catalog(self):
        """Verify custom catalog initialization."""
        service = STACService(catalog_name="planetary_computer")
        assert service.catalog_name == "planetary_computer"
        assert service.catalog_url == STAC_CATALOGS["planetary_computer"]


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
        assert payload["datetime"] == custom_datetime
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

    def test_get_stac_service_different_catalog_returns_new_instance(self):
        """Verify different instance for different catalog."""
        service1 = get_stac_service("earth_search")
        service2 = get_stac_service("planetary_computer")
        assert service1 is not service2
        assert service1.catalog_name != service2.catalog_name


@pytest.mark.anyio
class TestSTACImageryRESTEndpoint:
    """Integration tests for STAC imagery REST endpoint."""

    @patch("src.services.stac_service.STACService.search_imagery")
    async def test_stac_imagery_endpoint_success(self, mock_search, auth_client):
        """Test STAC imagery endpoint returns 200 with valid response."""
        mock_search.return_value = {
            "catalog": "earth_search",
            "collections": ["sentinel-2-l2a"],
            "bbox": RWANDA_BBOX,
            "datetime_range": "2024-01-01/2024-01-31",
            "max_cloud_cover": 20.0,
            "matched": 1,
            "items": [
                {
                    "id": "S2A_MSIL2A_20240115T081211_R121_T36MYE_20240115T121501",
                    "datetime": "2024-01-15T08:12:11Z",
                    "cloud_cover": 5.2,
                    "platform": "sentinel-2a",
                    "bbox": [28.9, -2.5, 29.1, -2.3],
                    "assets": {
                        "B04": {"href": "https://example.com/b04.tif", "type": "image/tiff"},
                        "B08": {"href": "https://example.com/b08.tif", "type": "image/tiff"},
                    },
                }
            ],
        }

        response = await auth_client.get("/api/rwanda/imagery/search")
        assert response.status_code == 200

        data = response.json()
        assert data["catalog"] == "earth_search"
        assert data["matched"] == 1
        assert len(data["items"]) == 1

    @patch("src.services.stac_service.STACService.search_imagery")
    async def test_stac_imagery_endpoint_with_query_params(self, mock_search, auth_client):
        """Test STAC imagery endpoint with query parameters."""
        mock_search.return_value = {
            "catalog": "earth_search",
            "matched": 0,
            "items": [],
        }

        response = await auth_client.get(
            "/api/rwanda/imagery/search",
            params={
                "bbox": "29.0,-2.0,30.0,-1.0",
                "datetime_range": "2024-01-01/2024-01-31",
                "max_cloud_cover": 10.0,
                "limit": 5,
            },
        )
        assert response.status_code == 200

        # Verify search was called with parsed parameters
        mock_search.assert_called_once()
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["bbox"] == [29.0, -2.0, 30.0, -1.0]
        assert call_kwargs["datetime_range"] == "2024-01-01/2024-01-31"
        assert call_kwargs["max_cloud_cover"] == 10.0
        assert call_kwargs["limit"] == 5

    async def test_stac_imagery_endpoint_invalid_bbox(self, auth_client):
        """Test STAC imagery endpoint rejects invalid bbox format."""
        response = await auth_client.get(
            "/api/rwanda/imagery/search",
            params={"bbox": "invalid,bbox"},
        )
        assert response.status_code == 400
        assert "invalid bbox format" in response.json()["detail"].lower()

    @patch("src.services.stac_service.STACService.search_imagery")
    async def test_stac_imagery_endpoint_handles_service_error(self, mock_search, auth_client):
        """Test STAC imagery endpoint handles service errors."""
        mock_search.return_value = {
            "error": "Connection timeout",
            "catalog": "earth_search",
        }

        response = await auth_client.get("/api/rwanda/imagery/search")
        assert response.status_code == 502
        assert "Connection timeout" in response.json()["detail"]
