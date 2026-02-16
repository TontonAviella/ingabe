import pytest
from pathlib import Path


@pytest.fixture
async def test_setup(auth_client):
    project_data = {
        "title": "Test Remove Layer Map",
        "description": "A test map for removing layers",
    }
    response = await auth_client.post("/api/maps/create", json=project_data)
    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_data = response.json()
    map_id = map_data["id"]
    return {"map_id": map_id}


@pytest.mark.anyio
async def test_remove_layer_from_map(test_setup, auth_client):
    map_id = test_setup["map_id"]

    file_path = Path(__file__).parent.parent / "test_fixtures" / "airports.fgb"

    with open(file_path, "rb") as f:
        upload_response = await auth_client.post(
            f"/api/maps/{map_id}/layers",
            files={"file": (file_path.name, f, "application/octet-stream")},
            data={"layer_name": "Vector Layer"},
        )
        assert upload_response.status_code == 200
        upload_data = upload_response.json()
        layer_id = upload_data["id"]
        child_map_id = upload_data["dag_child_map_id"]

    # Check the child map has the layer
    layers_response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert layers_response.status_code == 200
    layers_data = layers_response.json()
    assert "layers" in layers_data
    assert len(layers_data["layers"]) == 1

    # Remove the layer from the child map
    remove_response = await auth_client.delete(
        f"/api/maps/{child_map_id}/layer/{layer_id}"
    )

    assert remove_response.status_code == 200
    remove_data = remove_response.json()
    assert "message" in remove_data
    assert "layer_id" in remove_data
    assert "dag_child_map_id" in remove_data
    assert "dag_parent_map_id" in remove_data
    assert "layer_name" in remove_data
    assert remove_data["layer_id"] == layer_id
    assert remove_data["dag_parent_map_id"] == child_map_id
    assert remove_data["dag_child_map_id"] != child_map_id
    assert "successfully removed" in remove_data["message"].lower()

    # Remove operation creates a new child map due to DAG architecture
    removal_child_map_id = remove_data["dag_child_map_id"]
    layers_response_after = await auth_client.get(
        f"/api/maps/{removal_child_map_id}/layers"
    )
    assert layers_response_after.status_code == 200

    layers_data_after = layers_response_after.json()
    assert len(layers_data_after["layers"]) == 0

    # Verify the parent map (child_map_id) still contains the layer (DAG immutability)
    parent_layers_response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert parent_layers_response.status_code == 200
    parent_layers_data = parent_layers_response.json()
    assert len(parent_layers_data["layers"]) == 1
    assert parent_layers_data["layers"][0]["id"] == layer_id


@pytest.mark.anyio
async def test_remove_nonexistent_layer(test_setup, auth_client):
    map_id = test_setup["map_id"]
    fake_layer_id = "nonexistentid123"
    remove_response = await auth_client.delete(
        f"/api/maps/{map_id}/layer/{fake_layer_id}"
    )
    assert remove_response.status_code == 404
