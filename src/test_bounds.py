import pytest
import os


@pytest.fixture
async def test_setup(auth_client):
    project_data = {
        "title": "Bounds Test Map",
        "description": "Test map for bounds extraction",
        "link_accessible": True,
    }
    response = await auth_client.post("/api/maps/create", json=project_data)
    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_data = response.json()
    map_id = map_data["id"]
    return {"map_id": map_id}


@pytest.mark.anyio
async def test_fgb_bounds(test_setup, auth_client):
    fgb_file = os.path.join(
        os.path.dirname(__file__), "..", "test_fixtures", "UScounties.fgb"
    )
    map_id = test_setup["map_id"]
    assert os.path.exists(fgb_file), f"Test file {fgb_file} not found"
    expected_bounds = [-179.1743, 18.9103, 179.7739, 71.3892]
    with open(fgb_file, "rb") as f:
        response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": (os.path.basename(fgb_file), f, "application/octet-stream")},
            data={"layer_name": "US Counties"},
        )
    assert response.status_code == 200, f"Failed to upload FGB file: {response.text}"

    # Get the child map ID from the upload response
    upload_response = response.json()
    child_map_id = upload_response["dag_child_map_id"]

    response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert response.status_code == 200
    layers = response.json()["layers"]
    layer = None
    for layer_item in layers:
        if layer_item["name"] == "US Counties":
            layer = layer_item
            break
    assert layer is not None, "Layer not found in response"
    assert layer["bounds"] is not None, "Bounds are missing"

    tolerance = 1.3
    for i, val in enumerate(layer["bounds"]):
        assert abs(val - expected_bounds[i]) < tolerance, (
            f"Bounds[{i}] mismatch: Expected {expected_bounds[i]}, got {val}"
        )
