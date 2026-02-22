import pytest
from pathlib import Path

from src.postgis_tiles import MVT_LAYER_NAME


@pytest.fixture
async def test_map_with_layer(auth_client):
    map_response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Set Layer Style Test Map",
        },
    )
    assert map_response.status_code == 200, f"Failed to create map: {map_response.text}"
    map_id = map_response.json()["id"]

    file_path = str(
        Path(__file__).parent.parent.parent / "test_fixtures" / "coho_range.gpkg"
    )
    with open(file_path, "rb") as f:
        layer_response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": ("coho_range.gpkg", f, "application/octet-stream")},
            data={"layer_name": "Coho Salmon Range"},
        )
        assert layer_response.status_code == 200, (
            f"Failed to upload layer: {layer_response.text}"
        )
        layer_data = layer_response.json()
        layer_id = layer_data["id"]
        child_map_id = layer_data["dag_child_map_id"]

        return {
            "map_id": map_id,
            "child_map_id": child_map_id,
            "layer_id": layer_id,
        }


@pytest.mark.anyio
async def test_set_layer_style_success(auth_client, test_map_with_layer):
    layer_id = test_map_with_layer["layer_id"]
    map_id = test_map_with_layer["child_map_id"]

    style_request = {
        "maplibre_json_layers": [
            {
                "id": f"{layer_id}-fill",
                "type": "fill",
                "source": layer_id,
                "paint": {
                    "fill-color": "#FF6B6B",
                    "fill-opacity": 0.69,
                    "fill-outline-color": "#000000",
                },
            }
        ],
        "map_id": map_id,
    }

    style_response = await auth_client.post(
        f"/api/layers/{layer_id}/style", json=style_request
    )

    assert style_response.status_code == 200, (
        f"Failed to set style: {style_response.text}"
    )
    style_data = style_response.json()
    assert "style_id" in style_data
    assert style_data["layer_id"] == layer_id

    map_style_response = await auth_client.get(f"/api/maps/{map_id}/style.json")
    assert map_style_response.status_code == 200, (
        f"Failed to get style.json: {map_style_response.text}"
    )

    style_json = map_style_response.json()

    found_layer = None
    for layer in style_json.get("layers", []):
        if layer.get("id") == f"{layer_id}-fill":
            found_layer = layer
            break

    assert found_layer is not None, f"Layer {layer_id} not found in style.json"
    assert found_layer["type"] == "fill"
    assert found_layer["source"] == layer_id
    assert found_layer["source-layer"] == MVT_LAYER_NAME
    assert found_layer["paint"]["fill-color"] == "#FF6B6B"
    assert found_layer["paint"]["fill-opacity"] == 0.69


@pytest.mark.anyio
async def test_set_layer_style_invalid_source(auth_client, test_map_with_layer):
    layer_id = test_map_with_layer["layer_id"]
    map_id = test_map_with_layer["child_map_id"]

    style_request = {
        "maplibre_json_layers": [
            {
                "id": f"{layer_id}",
                "type": "fill",
                "source": "Lwrongsource",
                "paint": {"fill-color": "#FF6B6B", "fill-opacity": 0.7},
            }
        ],
        "map_id": map_id,
    }

    style_response = await auth_client.post(
        f"/api/layers/{layer_id}/style", json=style_request
    )

    assert style_response.status_code == 400
    error_detail = style_response.json()["detail"]
    assert "Layer source must be" in error_detail
    assert layer_id in error_detail


@pytest.mark.anyio
async def test_set_layer_style_invalid_layers_type(auth_client, test_map_with_layer):
    layer_id = test_map_with_layer["layer_id"]
    map_id = test_map_with_layer["child_map_id"]

    style_request = {"maplibre_json_layers": "not_an_array", "map_id": map_id}

    style_response = await auth_client.post(
        f"/api/layers/{layer_id}/style", json=style_request
    )

    assert style_response.status_code == 422
    error_detail = style_response.json()["detail"]
    assert len(error_detail) > 0


@pytest.mark.anyio
async def test_set_layer_style_non_dict_entry(auth_client, test_map_with_layer):
    """Non-dict entries in maplibre_json_layers should return 400."""
    layer_id = test_map_with_layer["layer_id"]
    map_id = test_map_with_layer["child_map_id"]

    style_request = {
        "maplibre_json_layers": ["not a dict object"],
        "map_id": map_id,
    }

    style_response = await auth_client.post(
        f"/api/layers/{layer_id}/style", json=style_request
    )
    # Pydantic may reject before route logic; accept 400 or 422
    assert style_response.status_code in (400, 422)


@pytest.mark.anyio
async def test_set_layer_style_nonexistent_layer(auth_client, test_map_with_layer):
    """Setting style on a non-existent layer returns 404."""
    map_id = test_map_with_layer["child_map_id"]

    style_request = {
        "maplibre_json_layers": [
            {
                "id": "L_NONEXIST-fill",
                "type": "fill",
                "source": "L_NONEXIST00",
                "paint": {"fill-color": "#000"},
            }
        ],
        "map_id": map_id,
    }

    style_response = await auth_client.post(
        "/api/layers/L_NONEXIST00/style", json=style_request
    )
    assert style_response.status_code == 404


@pytest.mark.anyio
async def test_set_layer_style_empty_layers_array(auth_client, test_map_with_layer):
    """Empty maplibre_json_layers array should be handled gracefully."""
    layer_id = test_map_with_layer["layer_id"]
    map_id = test_map_with_layer["child_map_id"]

    style_request = {
        "maplibre_json_layers": [],
        "map_id": map_id,
    }

    style_response = await auth_client.post(
        f"/api/layers/{layer_id}/style", json=style_request
    )
    # Empty array is either rejected or accepted — either is valid behavior
    assert style_response.status_code in (200, 400, 422)
