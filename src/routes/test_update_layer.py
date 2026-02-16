import pytest
from pathlib import Path


@pytest.fixture(scope="session")
async def test_map_with_layer(auth_client):
    map_response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Update Layer Test Map",
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
            data={"layer_name": "Original Layer Name"},
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
async def test_patch_layer_name_update_success(auth_client, test_map_with_layer):
    layer_id = test_map_with_layer["layer_id"]

    update_request = {"name": "Updated Layer Name"}

    response = await auth_client.patch(f"/api/layer/{layer_id}", json=update_request)

    assert response.status_code == 200, f"Failed to update layer: {response.text}"

    response_data = response.json()
    assert response_data["layer_id"] == layer_id
    assert response_data["name"] == "Updated Layer Name"

    # Verify the layer name was actually updated by checking describe endpoint
    describe_response = await auth_client.get(f"/api/layer/{layer_id}/describe")
    assert describe_response.status_code == 200
    describe_content = describe_response.text
    assert "Updated Layer Name" in describe_content


@pytest.mark.anyio
async def test_patch_layer_nonexistent_layer(auth_client):
    fake_layer_id = "L123456789AB"

    update_request = {"name": "Should Fail"}

    response = await auth_client.patch(
        f"/api/layer/{fake_layer_id}", json=update_request
    )

    assert response.status_code == 404
