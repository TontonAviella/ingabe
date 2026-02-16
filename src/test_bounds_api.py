import pytest
import os


@pytest.fixture
async def test_setup(auth_client):
    payload = {
        "title": "Bounds Test Map",
        "description": "Test map for bounds extraction",
    }
    response = await auth_client.post("/api/maps/create", json=payload)
    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_id = response.json()["id"]
    return {"map_id": map_id}


@pytest.mark.anyio
async def test_fgb_bounds_extraction(test_setup, auth_client):
    map_id = test_setup["map_id"]
    file_path = "test_fixtures/mixed.fgb"
    file_size = os.path.getsize(file_path)
    print(f"Testing with file: {file_path}, size: {file_size} bytes")
    expected_bounds = [-122.514713, 37.742305, -122.322322, 37.813824]
    with open(file_path, "rb") as f:
        response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": ("mixed.fgb", f, "application/octet-stream")},
            data={"layer_name": "San Francisco Streets"},
        )
        assert response.status_code == 200, f"Failed to upload layer: {response.text}"
        upload_response = response.json()
        layer_id = upload_response["id"]
        child_map_id = upload_response["dag_child_map_id"]
        print(f"Created layer with ID: {layer_id}")
    response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers_response = response.json()
    print(f"Layers response: {layers_response}")
    layers = layers_response["layers"]
    layer = next(
        (layer_item for layer_item in layers if layer_item["id"] == layer_id),
        layers[0] if layers else None,
    )
    assert layer is not None, "Layer not found in response"
    print(f"Found layer: {layer}")
    assert layer["bounds"] is not None, "Bounds are missing"
    tolerance = 0.0001
    for i, val in enumerate(layer["bounds"]):
        assert abs(val - expected_bounds[i]) < tolerance, (
            f"Bounds[{i}] mismatch: Expected {expected_bounds[i]}, got {val}"
        )


@pytest.mark.anyio
async def test_gpkg_bounds_extraction(test_setup, auth_client):
    map_id = test_setup["map_id"]
    file_path = "test_fixtures/UScounties.gpkg"
    expected_bounds = [-179.14734, 17.884813, 179.777158, 71.352561]
    with open(file_path, "rb") as f:
        response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": ("UScounties.gpkg", f, "application/octet-stream")},
            data={"layer_name": "US Counties"},
        )
        assert response.status_code == 200, f"Failed to upload layer: {response.text}"
        upload_response = response.json()
        child_map_id = upload_response["dag_child_map_id"]
    response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers = response.json()["layers"]
    layer = None
    for layer_item in layers:
        if layer_item["name"] == "US Counties":
            layer = layer_item
            break
    assert layer is not None, "Layer not found in response"
    assert layer["bounds"] is not None, "Bounds are missing"
    tolerance = 0.0001
    for i, val in enumerate(layer["bounds"]):
        assert abs(val - expected_bounds[i]) < tolerance, (
            f"Bounds[{i}] mismatch: Expected {expected_bounds[i]}, got {val}"
        )


@pytest.mark.anyio
async def test_view_map_with_bounds(test_setup, auth_client):
    map_id = test_setup["map_id"]
    file_path = "test_fixtures/mixed.fgb"
    with open(file_path, "rb") as f:
        response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": ("mixed.fgb", f, "application/octet-stream")},
            data={"layer_name": "San Francisco Streets"},
        )
        assert response.status_code == 200, f"Failed to upload layer: {response.text}"
        upload_response = response.json()
        child_map_id = upload_response["dag_child_map_id"]

    # Test viewing the map with bounds
    response = await auth_client.get(f"/api/maps/{child_map_id}")
    assert response.status_code == 200, f"Failed to get map: {response.text}"
    map_data = response.json()
    assert "layers" in map_data
    assert len(map_data["layers"]) > 0

    # Check that the layer has bounds
    layer = map_data["layers"][0]
    assert "bounds" in layer
    assert layer["bounds"] is not None
