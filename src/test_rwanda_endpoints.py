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

"""Integration tests for the Rwanda admin-unit and district NDVI endpoints."""

import json
import uuid
import pytest


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


H3_ADMIN_RUN_TAG = uuid.uuid4().hex[:8]


@pytest.mark.anyio
async def test_h3_admin_endpoints_reject_bad_input(auth_client):
    assert (await auth_client.get("/api/rwanda/h3/not-a-cell/admin")).status_code == 400
    assert (await auth_client.get("/api/rwanda/h3/836ad8fffffffff/admin")).status_code == 400  # resolution 3
    assert (await auth_client.get("/api/rwanda/admin/country/x/hexagons")).status_code == 400


@pytest.mark.anyio
async def test_h3_admin_endpoints_round_trip(auth_client):
    """A hexagon finds its village, the village finds its hexagons, and a coarser parent finds both."""
    import h3
    from src.structures import get_async_db_connection

    hexes = list(h3.cell_to_children(h3.latlng_to_cell(-1.9441, 30.0605, 8), 9))[:2]
    village = f"test-{H3_ADMIN_RUN_TAG}"
    rows = [(hx, "village", village, "Test village", 0.05, 0.5, 0.5) for hx in hexes]
    async with get_async_db_connection() as conn:
        await conn.executemany(
            "INSERT INTO h3_admin_overlap (h3_index, admin_level, unit_id, unit_name, overlap_km2,"
            " hex_fraction, unit_fraction) VALUES ($1, $2, $3, $4, $5, $6, $7)",
            rows,
        )
    try:
        units = (await auth_client.get(f"/api/rwanda/h3/{hexes[0]}/admin")).json()["units"]
        assert village in [u["id"] for u in units["village"]]

        parent = h3.cell_to_parent(hexes[0], 8)
        parent_units = (await auth_client.get(f"/api/rwanda/h3/{parent}/admin")).json()["units"]
        mine = [u for u in parent_units["village"] if u["id"] == village]
        assert mine and mine[0]["shared_km2"] == pytest.approx(0.1)

        body = (await auth_client.get(f"/api/rwanda/admin/village/{village}/hexagons")).json()
        assert body["count"] == 2
        assert sum(h["unit_fraction"] for h in body["hexagons"]) == pytest.approx(1.0)
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM h3_admin_overlap WHERE unit_id = $1", village)


@pytest.mark.anyio
async def test_admin_outlines_reject_bad_input(auth_client):
    assert (await auth_client.get("/api/rwanda/admin/country/outlines")).status_code == 400
    assert (await auth_client.get("/api/rwanda/admin/village/outlines")).status_code == 400  # no bbox
    assert (await auth_client.get("/api/rwanda/admin/cell/outlines", params={"bbox": "1,2,3"})).status_code == 400


@pytest.mark.anyio
async def test_admin_outlines_in_view(auth_client):
    r = await auth_client.get("/api/rwanda/admin/sector/outlines", params={"bbox": "30.0,-2.0,30.1,-1.9"})
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "FeatureCollection" and body["level"] == "sector"
    for f in body["features"]:
        assert f["properties"]["level"] == "sector" and "district" in f["properties"]


@pytest.mark.anyio
async def test_district_ndvi_map_says_which_levels_have_values(auth_client):
    r = await auth_client.get("/api/rwanda/ndvi/districts")
    assert r.status_code == 200
    levels = {x["level"]: x for x in r.json()["levels"]}
    assert levels["district"]["has_values"] is True
    assert levels["village"] == {"level": "village", "has_values": False, "values_from": "district"}
