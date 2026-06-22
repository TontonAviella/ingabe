from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services.admin_h3 import AdminH3Error, AdminH3Options, admin_geojson_to_h3


def _sample_admin_feature_collection() -> dict:
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "district_id": "D001",
                    "district_name": "Demo District",
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [30.0, -2.0],
                        [30.02, -2.0],
                        [30.02, -1.98],
                        [30.0, -1.98],
                        [30.0, -2.0],
                    ]],
                },
            }
        ],
    }


def test_admin_geojson_to_h3_builds_overlap_crosswalk():
    result = admin_geojson_to_h3(
        _sample_admin_feature_collection(),
        options=AdminH3Options(
            resolution=9,
            admin_level="district",
            id_property="district_id",
            name_property="district_name",
        ),
    )

    assert result["type"] == "FeatureCollection"
    assert result["features"]
    assert result["metadata"]["h3_resolution"] == 9

    feature = result["features"][0]
    props = feature["properties"]
    assert props["admin_id"] == "D001"
    assert props["admin_name"] == "Demo District"
    assert props["admin_level"] == "district"
    assert props["h3_resolution"] == 9
    assert 0 < props["overlap_ratio"] <= 1
    assert props["intersection_area_m2"] > 0
    assert props["hex_area_m2"] > 0


def test_admin_geojson_to_h3_outputs_geojson_coordinate_order_and_closed_ring():
    result = admin_geojson_to_h3(
        _sample_admin_feature_collection(),
        options=AdminH3Options(resolution=9, admin_level="district"),
    )

    coords = result["features"][0]["geometry"]["coordinates"][0]
    assert coords[0] == coords[-1]

    lng, lat = coords[0]
    assert 29 <= lng <= 31
    assert -3 <= lat <= 0


def test_admin_geojson_to_h3_can_return_membership_without_geometry():
    result = admin_geojson_to_h3(
        _sample_admin_feature_collection(),
        options=AdminH3Options(resolution=9, include_geometry=False),
    )

    assert result["features"]
    assert result["features"][0]["geometry"] is None
    assert result["metadata"]["geometry_included"] is False


def test_admin_geojson_to_h3_enforces_safety_limit():
    with pytest.raises(AdminH3Error, match="above limit"):
        admin_geojson_to_h3(
            _sample_admin_feature_collection(),
            options=AdminH3Options(resolution=9, max_hexes=1),
        )


@pytest.mark.anyio
async def test_admin_h3_polyfill_endpoint(auth_client):
    mock_hex_ids = {"896ad8136cbffff"}

    def mock_cell_to_boundary(hex_id):
        return [
            (-1.995, 30.005),
            (-1.990, 30.010),
            (-1.985, 30.005),
            (-1.985, 29.995),
            (-1.990, 29.990),
            (-1.995, 29.995),
        ]

    with patch(
        "src.services.geokernel_client.admin_geojson_to_h3_via_geokernel",
        new=AsyncMock(return_value=None),
    ):
        with patch(
            "src.services.admin_h3.h3.geo_to_cells",
            return_value=mock_hex_ids,
        ):
            with patch(
                "src.services.admin_h3.h3.cell_to_boundary",
                side_effect=mock_cell_to_boundary,
            ):
                response = await auth_client.post(
                    "/api/rwanda/grid/h3/admin-polyfill",
                    json={
                        "geojson": _sample_admin_feature_collection(),
                        "resolution": 9,
                        "admin_level": "district",
                        "id_property": "district_id",
                        "name_property": "district_name",
                    },
                )

    assert response.status_code == 200
    data = response.json()
    assert data["features"][0]["properties"]["h3_index"] == "896ad8136cbffff"
    assert data["features"][0]["properties"]["admin_name"] == "Demo District"
