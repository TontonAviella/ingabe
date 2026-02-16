import pytest


@pytest.mark.anyio
async def test_upload_raster_with_projection_detection(auth_client):
    """Test that raster upload detects and stores projection information."""

    # Create a map first
    map_response = await auth_client.post(
        "/api/maps/create", json={"title": "Test Projection Map"}
    )
    assert map_response.status_code == 200
    map_id = map_response.json()["id"]

    # Upload the losangeles-dem_26711.tif file which has EPSG:26711
    with open("test_fixtures/losangeles-dem_26711.tif", "rb") as f:
        files = {"file": ("losangeles-dem_26711.tif", f, "image/tiff")}
        data = {"layer_name": "LA DEM UTM 11N"}

        upload_response = await auth_client.post(
            f"/api/maps/{map_id}/layers", files=files, data=data
        )

    assert upload_response.status_code == 200
    upload_data = upload_response.json()
    layer_id = upload_data["id"]
    child_map_id = upload_data["dag_child_map_id"]

    # Get the layer list and check that projection info is included
    layers_response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert layers_response.status_code == 200

    layers_data = layers_response.json()
    layers = layers_data["layers"]
    assert len(layers) == 1

    layer = layers[0]
    assert layer["id"] == layer_id
    assert layer["name"] == "LA DEM UTM 11N"
    assert layer["type"] == "raster"

    # Check that the original SRID was detected and stored
    assert layer["original_srid"] == 26711

    # Verify it's also in the metadata
    assert "original_srid" in layer["metadata"]
    assert layer["metadata"]["original_srid"] == 26711


@pytest.mark.anyio
async def test_upload_vector_with_projection_detection(auth_client):
    """Test that vector upload detects and stores projection information."""

    # Create a map first
    map_response = await auth_client.post(
        "/api/maps/create", json={"title": "Test Vector Projection Map"}
    )
    assert map_response.status_code == 200
    map_id = map_response.json()["id"]

    # Upload the Banff trails shapefile ZIP which has EPSG:26911
    with open("test_fixtures/banffopendata_trails.zip", "rb") as f:
        files = {"file": ("banffopendata_trails.zip", f, "application/zip")}
        data = {"layer_name": "Banff Trails"}

        upload_response = await auth_client.post(
            f"/api/maps/{map_id}/layers", files=files, data=data
        )

    assert upload_response.status_code == 200
    upload_data = upload_response.json()
    layer_id = upload_data["id"]
    child_map_id = upload_data["dag_child_map_id"]

    # Get the layer list and check that projection info is included
    layers_response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert layers_response.status_code == 200

    layers_data = layers_response.json()
    layers = layers_data["layers"]
    assert len(layers) == 1

    layer = layers[0]
    assert layer["id"] == layer_id
    assert layer["name"] == "Banff Trails"
    assert layer["type"] == "vector"

    # Check that the original SRID was detected as EPSG:26911 (NAD83 / UTM zone 11N)
    assert "original_srid" in layer
    assert layer["original_srid"] == 26911

    # Verify it's also in the metadata
    assert "original_srid" in layer["metadata"]
    assert layer["metadata"]["original_srid"] == 26911


@pytest.mark.anyio
async def test_upload_geojson_with_wgs84(auth_client):
    """Test that GeoJSON files with WGS84 projection are detected properly."""

    # Create a map first
    map_response = await auth_client.post(
        "/api/maps/create", json={"title": "Test GeoJSON WGS84 Map"}
    )
    assert map_response.status_code == 200
    map_id = map_response.json()["id"]

    # Upload a GeoJSON file (airports.geojson) which has EPSG:4326
    with open("test_fixtures/airports.geojson", "rb") as f:
        files = {"file": ("airports.geojson", f, "application/json")}
        data = {"layer_name": "Airports"}

        upload_response = await auth_client.post(
            f"/api/maps/{map_id}/layers", files=files, data=data
        )

    assert upload_response.status_code == 200
    upload_data = upload_response.json()
    layer_id = upload_data["id"]
    child_map_id = upload_data["dag_child_map_id"]

    # Get the layer list
    layers_response = await auth_client.get(f"/api/maps/{child_map_id}/layers")
    assert layers_response.status_code == 200

    layers_data = layers_response.json()
    layers = layers_data["layers"]
    assert len(layers) == 1

    layer = layers[0]
    assert layer["id"] == layer_id
    assert layer["name"] == "Airports"
    assert layer["type"] == "vector"

    # Check that original_srid is 4326 for WGS84 GeoJSON
    assert layer["original_srid"] == 4326

    # Verify it's also in the metadata
    assert "original_srid" in layer["metadata"]
    assert layer["metadata"]["original_srid"] == 4326
