import pytest
import os
import tempfile
import subprocess
import re


@pytest.fixture
async def test_map_id(auth_client):
    payload = {
        "title": "LAZ Upload Test Map",
        "description": "Test map for LAZ file upload",
    }

    response = await auth_client.post(
        "/api/maps/create",
        json=payload,
    )

    assert response.status_code == 200, f"Failed to create map: {response.text}"
    map_id = response.json()["id"]

    return map_id


@pytest.mark.anyio
async def test_laz_file_upload(test_map_id, auth_client):
    file_path = "test_fixtures/whitney_pc.laz"

    file_size = os.path.getsize(file_path)
    print(f"Testing with LAZ file: {file_path}, size: {file_size} bytes")

    with open(file_path, "rb") as f:
        files = {"file": ("whitney_pc.laz", f)}
        data = {
            "layer_name": "Whitney Point Cloud",
        }

        response = await auth_client.post(
            f"/api/maps/{test_map_id}/layers",
            files=files,
            data=data,
        )

        assert response.status_code == 200, (
            f"Failed to upload LAZ file: {response.text}"
        )
        response_data = response.json()
        layer_id = response_data["id"]
        dag_child_map_id = response_data["dag_child_map_id"]
        print(f"Created LAZ layer with ID: {layer_id}")
        print(f"DAG child map ID: {dag_child_map_id}")

    response = await auth_client.get(
        f"/api/maps/{dag_child_map_id}/layers",
    )
    assert response.status_code == 200, f"Failed to get layers: {response.text}"
    layers_response = response.json()
    print(f"Layers response: {layers_response}")

    layers = layers_response["layers"]

    assert any(layer["id"] == layer_id for layer in layers), (
        "LAZ layer was not added to map"
    )

    layer_data = next(layer for layer in layers if layer["id"] == layer_id)
    assert layer_data["name"] == "Whitney Point Cloud"
    assert layer_data["type"] == "point_cloud"

    # Check metadata
    metadata = layer_data["metadata"]
    assert metadata["original_filename"] == "whitney_pc.laz"

    # Check pointcloud_anchor (lon, lat)
    anchor = metadata["pointcloud_anchor"]
    assert abs(anchor["lon"] - (-118.29092)) < 0.00001  # longitude
    assert abs(anchor["lat"] - 36.57497) < 0.00001  # latitude

    # Check pointcloud_z_range
    z_range = metadata["pointcloud_z_range"]
    assert abs(z_range[0] - 3687.58) < 0.01  # min z
    assert abs(z_range[1] - 5770.25) < 0.01  # max z

    # Check bounds to some significant figures
    bounds = layer_data["bounds"]
    assert abs(bounds[0] - (-118.2964)) < 0.0001  # min lon
    assert abs(bounds[1] - 36.57041) < 0.00001  # min lat
    assert abs(bounds[2] - (-118.2854)) < 0.0001  # max lon
    assert abs(bounds[3] - 36.57954) < 0.00001  # max lat

    # Download the LAZ file using get_layer_laz endpoint
    response = await auth_client.get(f"/api/layer/{layer_id}.laz")

    assert response.status_code == 200, f"Failed to download LAZ file: {response.text}"

    # Check that the file size is over 1.5MB as expected
    file_size_mb = len(response.content) / (1024 * 1024)
    print(f"Downloaded LAZ file size: {file_size_mb:.2f} MB")
    assert file_size_mb > 1.5, f"Expected file size > 1.5MB, got {file_size_mb:.2f} MB"

    # Save the downloaded content to a temporary file
    with tempfile.NamedTemporaryFile(suffix=".laz") as temp_file:
        temp_file.write(response.content)
        temp_file_path = temp_file.name

        # Run lasinfo64 on the downloaded file
        result = subprocess.run(
            ["lasinfo64", temp_file_path], capture_output=True, text=True, check=True
        )

        print("lasinfo64 output:")
        print(result.stdout)
        # print(result.stderr)

        # Verify the expected characteristics from the las2las conversion

        output = result.stdout + result.stderr

        # Check LAS version is 1.3 (set by -set_version 1.3)
        assert re.search(r"version major\.minor:\s*1\.3", output), (
            "Expected LAS version 1.3"
        )

        # Check coordinate system is WGS84/EPSG:4326 (set by -proj_epsg 4326)
        assert 'GEOGCS["WGS 84"' in output, "Expected WGS84 coordinate system"
        assert 'AUTHORITY["EPSG","4326"]' in output, "Expected EPSG:4326 authority"

        # Check point count is preserved (207905 points)
        assert re.search(r"number of point records:\s*207905", output), (
            "Expected 207905 point records"
        )

        # Check offset coordinates are approximately correct for WGS84
        # The original offset was likely in a different CRS, after reprojection to WGS84
        # we expect offset around 36 -118 0 (longitude, latitude, elevation)
        assert re.search(r"offset x y z:\s*36\s+-118\s+0", output), (
            "Expected offset coordinates for WGS84"
        )

        # Verify the coordinate bounds make sense for WGS84 (longitude/latitude)
        # Should be around longitude -118.xx and latitude 36.xx
        assert re.search(r"min x y z:", output) and re.search(r"max x y z:", output), (
            "Expected coordinate bounds"
        )
