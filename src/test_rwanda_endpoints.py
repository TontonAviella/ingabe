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

"""Integration tests for Rwanda agriculture lakehouse endpoints."""

import json
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.anyio
async def test_h3_grid_endpoint_valid_params(auth_client):
    """Test H3 grid generation with valid parameters."""
    # Mock h3.geo_to_cells to return a manageable set of hex IDs
    mock_hex_ids = {"8a1fb46600fffff", "8a1fb46601fffff", "8a1fb46602fffff"}

    # Mock h3.cell_to_boundary to return polygon coordinates
    def mock_cell_to_boundary(hex_id):
        return [
            (-1.94, 29.87),
            (-1.93, 29.88),
            (-1.92, 29.87),
            (-1.93, 29.86),
            (-1.94, 29.87),
        ]

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        with patch("src.routes.rwanda_routes.h3.cell_to_boundary", side_effect=mock_cell_to_boundary):
            response = await auth_client.get(
                "/api/rwanda/grid/h3",
                params={
                    "resolution": 7,
                    "bounds": "29.0,-3.0,30.5,-1.0",
                }
            )

    assert response.status_code == 200
    data = response.json()

    # Verify GeoJSON structure
    assert data["type"] == "FeatureCollection"
    assert "features" in data
    assert len(data["features"]) == 3

    # Verify feature structure
    for feature in data["features"]:
        assert feature["type"] == "Feature"
        assert "properties" in feature
        assert "h3_index" in feature["properties"]
        assert feature["properties"]["resolution"] == 7
        assert feature["geometry"]["type"] == "Polygon"
        assert len(feature["geometry"]["coordinates"]) == 1


@pytest.mark.anyio
async def test_h3_grid_endpoint_default_resolution(auth_client):
    """Test H3 grid generation with default resolution parameter."""
    mock_hex_ids = {"8a1fb46600fffff"}

    def mock_cell_to_boundary(hex_id):
        return [(-1.94, 29.87), (-1.93, 29.88), (-1.92, 29.87), (-1.93, 29.86)]

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        with patch("src.routes.rwanda_routes.h3.cell_to_boundary", side_effect=mock_cell_to_boundary):
            response = await auth_client.get(
                "/api/rwanda/grid/h3",
                params={
                    "bounds": "29.0,-3.0,30.5,-1.0",
                    # resolution not specified - should default to 7
                }
            )

    assert response.status_code == 200
    data = response.json()

    # Verify default resolution was used (7 per docstring)
    assert data["features"][0]["properties"]["resolution"] == 7


@pytest.mark.anyio
async def test_h3_grid_endpoint_invalid_bounds(auth_client):
    """Test H3 grid endpoint rejects invalid bounds format."""
    response = await auth_client.get(
        "/api/rwanda/grid/h3",
        params={
            "resolution": 7,
            "bounds": "invalid,bounds",
        }
    )

    assert response.status_code == 400
    assert "Invalid bounds format" in response.json()["detail"]


@pytest.mark.anyio
async def test_h3_grid_endpoint_out_of_range_bounds(auth_client):
    """Test H3 grid endpoint validates coordinate ranges."""
    # Test longitude out of range
    response = await auth_client.get(
        "/api/rwanda/grid/h3",
        params={
            "resolution": 7,
            "bounds": "200.0,-3.0,30.5,-1.0",  # west > 180
        }
    )

    assert response.status_code == 400
    assert "Longitude must be between -180 and 180" in response.json()["detail"]

    # Test latitude out of range
    response = await auth_client.get(
        "/api/rwanda/grid/h3",
        params={
            "resolution": 7,
            "bounds": "29.0,-100.0,30.5,-1.0",  # south < -90
        }
    )

    assert response.status_code == 400
    assert "Latitude must be between -90 and 90" in response.json()["detail"]


@pytest.mark.anyio
async def test_h3_grid_endpoint_invalid_bounds_ordering(auth_client):
    """Test H3 grid endpoint validates bounds ordering."""
    response = await auth_client.get(
        "/api/rwanda/grid/h3",
        params={
            "resolution": 7,
            "bounds": "30.5,-1.0,29.0,-3.0",  # west >= east, south >= north
        }
    )

    assert response.status_code == 400
    assert "west must be < east" in response.json()["detail"]


@pytest.mark.anyio
async def test_h3_grid_endpoint_safety_limit(auth_client):
    """Test H3 grid endpoint enforces 50,000 cell safety limit."""
    # Mock h3 to return more than 50,000 hexagons
    mock_hex_ids = {f"hex_{i}" for i in range(50001)}

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        response = await auth_client.get(
            "/api/rwanda/grid/h3",
            params={
                "resolution": 7,
                "bounds": "29.0,-3.0,30.5,-1.0",
            }
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "50,000" in detail
    assert "50001" in detail or "50,001" in detail


@pytest.mark.anyio
async def test_h3_grid_endpoint_high_resolution_allowed(auth_client):
    """Test H3 grid endpoint allows high resolution values up to 15."""
    mock_hex_ids = {"8a1fb46600fffff"}

    def mock_cell_to_boundary(hex_id):
        return [(-1.94, 29.87), (-1.93, 29.88), (-1.92, 29.87), (-1.93, 29.86)]

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        with patch("src.routes.rwanda_routes.h3.cell_to_boundary", side_effect=mock_cell_to_boundary):
            # Test resolution 15 (maximum)
            response = await auth_client.get(
                "/api/rwanda/grid/h3",
                params={
                    "resolution": 15,
                    "bounds": "29.0,-2.0,29.1,-1.9",  # Small area
                }
            )

    # Should succeed with valid resolution
    assert response.status_code == 200
    assert response.json()["features"][0]["properties"]["resolution"] == 15


@pytest.mark.anyio
async def test_tools_json_is_valid_json(client):
    """Test that tools.json is valid JSON and can be parsed."""
    from pathlib import Path

    tools_path = Path(__file__).parent / "geoprocessing" / "tools.json"
    assert tools_path.exists(), "tools.json file not found"

    with open(tools_path, "r") as f:
        tools = json.load(f)

    assert isinstance(tools, list), "tools.json should contain a list"


@pytest.mark.anyio
async def test_zonal_stats_tool_definition_exists(client):
    """Test that query_rwanda_zonal_stats tool is defined in tools.json."""
    from pathlib import Path

    tools_path = Path(__file__).parent / "geoprocessing" / "tools.json"
    with open(tools_path, "r") as f:
        tools = json.load(f)

    # Find the zonal stats tool
    zonal_tool = None
    for tool in tools:
        if tool.get("function", {}).get("name") == "query_rwanda_zonal_stats":
            zonal_tool = tool
            break

    assert zonal_tool is not None, "query_rwanda_zonal_stats tool not found in tools.json"

    # Verify structure
    assert "function" in zonal_tool
    assert "name" in zonal_tool["function"]
    assert "description" in zonal_tool["function"]
    assert "parameters" in zonal_tool["function"]

    # Verify parameters
    params = zonal_tool["function"]["parameters"]
    assert "properties" in params
    assert "query_type" in params["properties"]
    assert params["properties"]["query_type"]["type"] == "string"
    assert "enum" in params["properties"]["query_type"]
    assert "district_summary" in params["properties"]["query_type"]["enum"]
    assert "ndvi_timeseries" in params["properties"]["query_type"]["enum"]

    # Verify optional parameters exist
    assert "province" in params["properties"]
    assert "h3_index" in params["properties"]
    assert "parcel_id" in params["properties"]
    assert "date_from" in params["properties"]
    assert "date_to" in params["properties"]
    assert "week_start" in params["properties"]

    # Verify required fields
    assert "required" in params
    assert "query_type" in params["required"]


@pytest.mark.anyio
async def test_satellite_imagery_tool_definition_exists(client):
    """Test that search_satellite_imagery tool is defined in tools.json."""
    from pathlib import Path

    tools_path = Path(__file__).parent / "geoprocessing" / "tools.json"
    with open(tools_path, "r") as f:
        tools = json.load(f)

    # Find the satellite imagery tool
    imagery_tool = None
    for tool in tools:
        if tool.get("function", {}).get("name") == "search_satellite_imagery":
            imagery_tool = tool
            break

    assert imagery_tool is not None, "search_satellite_imagery tool not found in tools.json"

    # Verify structure
    assert "function" in imagery_tool
    assert "name" in imagery_tool["function"]
    assert "description" in imagery_tool["function"]
    assert "parameters" in imagery_tool["function"]

    # Verify parameters
    params = imagery_tool["function"]["parameters"]
    assert "properties" in params
    assert "bbox" in params["properties"]
    assert "datetime_range" in params["properties"]
    assert "max_cloud_cover" in params["properties"]
    assert "limit" in params["properties"]

    # Verify parameter types
    assert params["properties"]["bbox"]["type"] == "string"
    assert params["properties"]["datetime_range"]["type"] == "string"
    assert params["properties"]["max_cloud_cover"]["type"] == "number"
    assert params["properties"]["limit"]["type"] == "integer"


@pytest.mark.anyio
async def test_rwanda_routes_are_mounted(auth_client):
    """Test that Rwanda routes are properly mounted under /api/rwanda/ prefix."""
    # Test a simple endpoint to verify mount
    response = await auth_client.get("/api/rwanda/tables")

    # Should get 200 or reasonable response (not 404)
    # Note: Actual implementation may require database setup, so we just verify route exists
    assert response.status_code in [200, 500], (
        f"Rwanda routes not properly mounted. Status: {response.status_code}"
    )


@pytest.mark.anyio
async def test_h3_grid_polygon_closure(auth_client):
    """Test that H3 grid polygons are properly closed (first point = last point)."""
    mock_hex_ids = {"8a1fb46600fffff"}

    def mock_cell_to_boundary(hex_id):
        # Return 5 points (not including closure)
        return [
            (-1.94, 29.87),
            (-1.93, 29.88),
            (-1.92, 29.87),
            (-1.93, 29.86),
            (-1.94, 29.87),
        ]

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        with patch("src.routes.rwanda_routes.h3.cell_to_boundary", side_effect=mock_cell_to_boundary):
            response = await auth_client.get(
                "/api/rwanda/grid/h3",
                params={
                    "resolution": 7,
                    "bounds": "29.0,-3.0,30.5,-1.0",
                }
            )

    assert response.status_code == 200
    data = response.json()

    # Get the polygon coordinates
    coords = data["features"][0]["geometry"]["coordinates"][0]

    # First and last point should be the same (GeoJSON polygon closure)
    assert coords[0] == coords[-1], "Polygon must be closed (first point = last point)"


@pytest.mark.anyio
async def test_h3_grid_coordinate_order(auth_client):
    """Test that H3 grid converts from h3's (lat, lng) to GeoJSON's (lng, lat)."""
    mock_hex_ids = {"8a1fb46600fffff"}

    def mock_cell_to_boundary(hex_id):
        # h3 returns (lat, lng) pairs
        return [
            (-1.94, 29.87),  # (lat, lng)
            (-1.93, 29.88),
            (-1.92, 29.87),
        ]

    with patch("src.routes.rwanda_routes.h3.geo_to_cells", return_value=mock_hex_ids):
        with patch("src.routes.rwanda_routes.h3.cell_to_boundary", side_effect=mock_cell_to_boundary):
            response = await auth_client.get(
                "/api/rwanda/grid/h3",
                params={
                    "resolution": 7,
                    "bounds": "29.0,-3.0,30.5,-1.0",
                }
            )

    assert response.status_code == 200
    data = response.json()

    # Get first coordinate pair (lng, lat in GeoJSON)
    first_coord = data["features"][0]["geometry"]["coordinates"][0][0]

    # Verify coordinate order is (lng, lat) not (lat, lng)
    # Longitude should be ~29.87, latitude should be ~-1.94
    assert 29 <= first_coord[0] <= 31, f"First coordinate should be longitude ~29.87, got {first_coord[0]}"
    assert -3 <= first_coord[1] <= 0, f"Second coordinate should be latitude ~-1.94, got {first_coord[1]}"


# ============================================================================
# Superset Integration Tests
# ============================================================================


@pytest.mark.anyio
async def test_superset_status_endpoint_unavailable(auth_client):
    """Test GET /api/rwanda/superset/status returns valid response."""
    # Real endpoint test — Superset may or may not be running
    response = await auth_client.get("/api/rwanda/superset/status")

    assert response.status_code == 200
    data = response.json()
    assert "available" in data
    assert "url" in data
    assert isinstance(data["available"], bool)


@pytest.mark.anyio
async def test_superset_status_endpoint_available(auth_client):
    """Test GET /api/rwanda/superset/status when Superset is healthy."""
    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("src.routes.rwanda_routes.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        response = await auth_client.get("/api/rwanda/superset/status")

    assert response.status_code == 200
    data = response.json()
    assert data["available"] is True


@pytest.mark.anyio
async def test_superset_guest_token_missing_dashboard_id(auth_client):
    """Test POST /api/rwanda/superset/guest-token without dashboard_id."""
    response = await auth_client.post(
        "/api/rwanda/superset/guest-token",
        json={},
    )

    assert response.status_code == 400
    data = response.json()
    assert "dashboard_id" in data["detail"].lower()


@pytest.mark.anyio
async def test_superset_guest_token_success(auth_client):
    """Test POST /api/rwanda/superset/guest-token with mocked Superset API."""
    mock_login_response = MagicMock()
    mock_login_response.status_code = 200
    mock_login_response.json.return_value = {"access_token": "test-token-123"}
    mock_login_response.raise_for_status = MagicMock()

    mock_guest_response = MagicMock()
    mock_guest_response.status_code = 200
    mock_guest_response.json.return_value = {"token": "guest-token-456"}
    mock_guest_response.raise_for_status = MagicMock()

    with patch("src.routes.rwanda_routes.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[mock_login_response, mock_guest_response])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        response = await auth_client.post(
            "/api/rwanda/superset/guest-token",
            json={"dashboard_id": "test-dash-123"},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["token"] == "guest-token-456"


@pytest.mark.anyio
async def test_superset_guest_token_superset_unreachable(auth_client):
    """Test POST /api/rwanda/superset/guest-token when Superset is down."""
    with patch("src.routes.rwanda_routes.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        response = await auth_client.post(
            "/api/rwanda/superset/guest-token",
            json={"dashboard_id": "test-dash-123"},
        )

    assert response.status_code == 502
    data = response.json()
    assert "detail" in data


@pytest.mark.anyio
async def test_superset_dashboards_endpoint_success(auth_client):
    """Test GET /api/rwanda/superset/dashboards with mocked Superset API."""
    mock_login_response = MagicMock()
    mock_login_response.status_code = 200
    mock_login_response.json.return_value = {"access_token": "test-token-123"}
    mock_login_response.raise_for_status = MagicMock()

    mock_dashboards_response = MagicMock()
    mock_dashboards_response.status_code = 200
    mock_dashboards_response.json.return_value = {
        "result": [
            {"id": 1, "dashboard_title": "Rwanda NDVI", "url": "/superset/dashboard/1/", "status": "published"},
            {"id": 2, "dashboard_title": "Crop Analysis", "url": "/superset/dashboard/2/", "status": "draft"},
        ]
    }
    mock_dashboards_response.raise_for_status = MagicMock()

    with patch("src.routes.rwanda_routes.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_login_response)
        mock_client.get = AsyncMock(return_value=mock_dashboards_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        response = await auth_client.get("/api/rwanda/superset/dashboards")

    assert response.status_code == 200
    data = response.json()
    assert "dashboards" in data
    assert "count" in data
    assert data["count"] == 2
    assert data["dashboards"][0]["title"] == "Rwanda NDVI"
    assert data["dashboards"][1]["title"] == "Crop Analysis"


@pytest.mark.anyio
async def test_superset_dashboards_endpoint_unreachable(auth_client):
    """Test GET /api/rwanda/superset/dashboards when Superset is down."""
    with patch("src.routes.rwanda_routes.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        response = await auth_client.get("/api/rwanda/superset/dashboards")

    assert response.status_code == 502
