import pytest


@pytest.fixture
async def test_map_id(auth_client):
    payload = {
        "title": "Upload CSV Test Map",
        "description": "Test map for CSV file upload",
    }

    response = await auth_client.post(
        "/api/maps/create",
        json=payload,
    )

    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_id = response.json()["id"]

    return map_id


@pytest.mark.anyio
async def test_upload_geocsv(test_map_id, auth_client):
    file_path = "test_fixtures/geocoded.csv"

    with open(file_path, "rb") as f:
        files = {"file": ("geocoded.csv", f)}
        data = {
            "layer_name": "CSV Points",
        }

        response = await auth_client.post(
            f"/api/maps/{test_map_id}/layers",
            files=files,
            data=data,
        )

        assert response.status_code == 200, f"Failed to upload CSV: {response.text}"
        response_data = response.json()
        layer_id = response_data["id"]
        dag_child_map_id = response_data["dag_child_map_id"]

    response = await auth_client.get(
        f"/api/maps/{dag_child_map_id}/layers",
    )
    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers_response = response.json()

    layers = layers_response["layers"]

    assert any(layer["id"] == layer_id for layer in layers), (
        "CSV layer was not added to map"
    )

    layer_data = next(layer for layer in layers if layer["id"] == layer_id)
    assert layer_data["name"] == "CSV Points"

    response = await auth_client.get(
        f"/api/layer/{layer_id}.geojson",
    )

    assert response.status_code == 200, f"Failed to access layer: {response.text}"
    assert response.headers["Content-Type"] == "application/geo+json"

    geojson = response.json()
    assert "features" in geojson
    assert len(geojson["features"]) == 3

    for feature in geojson["features"]:
        assert feature["type"] == "Feature"
        assert feature["geometry"]["type"] == "Point"
        assert "coordinates" in feature["geometry"]
        assert len(feature["geometry"]["coordinates"]) == 2
