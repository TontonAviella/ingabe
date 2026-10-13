import pytest
import os


@pytest.fixture
async def test_map_id(auth_client):
    payload = {
        "title": "Upload Without Adding Test Map",
        "description": "Test map for layer upload without adding to map",
    }

    response = await auth_client.post(
        "/api/maps/create",
        json=payload,
    )

    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_id = response.json()["id"]

    return map_id


@pytest.mark.anyio
async def test_upload_without_adding_to_map(test_map_id, auth_client):
    file_path = "test_fixtures/airports.fgb"

    file_size = os.path.getsize(file_path)
    print(f"Testing with file: {file_path}, size: {file_size} bytes")

    with open(file_path, "rb") as f:
        files = {"file": ("airports.fgb", f)}
        data = {
            "layer_name": "Airports",
            "add_layer_to_map": "false",
        }

        response = await auth_client.post(
            f"/api/maps/{test_map_id}/layers",
            files=files,
            data=data,
        )

        assert response.status_code == 200, f"Failed to upload layer: {response.text}"
        layer_id = response.json()["id"]
        print(f"Created layer with ID: {layer_id}")

    response = await auth_client.get(
        f"/api/maps/{test_map_id}/layers",
    )

    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers_response = response.json()
    print(f"Layers response: {layers_response}")

    layers = layers_response["layers"]

    assert not any(layer["id"] == layer_id for layer in layers), (
        "Layer was incorrectly added to map"
    )

    response = await auth_client.get(
        f"/api/layer/{layer_id}.geojson",
    )

    assert response.status_code == 200, f"Failed to access layer: {response.text}"
    assert response.headers["Content-Type"] == "application/geo+json"

    geojson = response.json()
    assert "features" in geojson
    assert len(geojson["features"]) > 0


@pytest.mark.anyio
async def test_upload_with_adding_to_map(test_map_id, auth_client):
    file_path = "test_fixtures/airports.fgb"

    with open(file_path, "rb") as f:
        files = {"file": ("airports.fgb", f)}
        data = {
            "layer_name": "Airports Default",
        }

        response = await auth_client.post(
            f"/api/maps/{test_map_id}/layers",
            files=files,
            data=data,
        )

        assert response.status_code == 200, f"Failed to upload layer: {response.text}"
        upload_response = response.json()
        layer_id = upload_response["id"]
        child_map_id = upload_response["dag_child_map_id"]
        print(f"Created layer with ID: {layer_id}")
        print(f"Created child map with ID: {child_map_id}")

    # Check the child map for the layer, not the parent map
    response = await auth_client.get(
        f"/api/maps/{child_map_id}/layers",
    )

    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers_response = response.json()

    layers = layers_response["layers"]

    assert any(layer["id"] == layer_id for layer in layers), (
        "Layer was not added to map"
    )

    # Verify the parent map does NOT contain the new layer (DAG immutability)
    parent_response = await auth_client.get(
        f"/api/maps/{test_map_id}/layers",
    )
    assert parent_response.status_code == 200, (
        f"Failed to get parent layers: {parent_response.text}"
    )
    parent_layers = parent_response.json()["layers"]
    assert len(parent_layers) == 0, "Parent map should not contain the uploaded layer"


@pytest.mark.anyio
async def test_an_uploaded_layer_can_be_added_to_its_map_later(test_map_id, auth_client):
    """PUT /maps/{map}/layer/{layer} adds the layer's id, not NULL, and refuses a second add (audit R1-34)."""
    with open("test_fixtures/airports.fgb", "rb") as f:
        response = await auth_client.post(
            f"/api/maps/{test_map_id}/layers",
            files={"file": ("airports.fgb", f)},
            data={"layer_name": "Airports later", "add_layer_to_map": "false"},
        )
    assert response.status_code == 200, response.text
    layer_id = response.json()["id"]

    added = await auth_client.put(f"/api/maps/{test_map_id}/layer/{layer_id}")
    assert added.status_code == 200, added.text
    assert added.json()["layer_id"] == layer_id

    from src.database.pool import get_async_db_connection

    async with get_async_db_connection() as conn:
        layers = await conn.fetchval("SELECT layers FROM user_mundiai_maps WHERE id = $1", test_map_id)
    assert layer_id in layers and None not in layers

    again = await auth_client.put(f"/api/maps/{test_map_id}/layer/{layer_id}")
    assert again.status_code == 400
