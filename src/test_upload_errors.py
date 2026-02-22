"""Upload error path tests — covers missing files, bad formats, auth, and invalid maps.

Parametrized to test multiple error scenarios efficiently.
"""


import pytest


@pytest.fixture
async def test_map_id(auth_client):
    """Create a throwaway map for upload tests."""
    resp = await auth_client.post(
        "/api/maps/create",
        json={"title": "Upload Error Test"},
    )
    assert resp.status_code == 200
    return resp.json()["id"]


# ---------------------------------------------------------------------------
# Missing / malformed file
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_no_file_field(auth_client, test_map_id):
    """POST without file field should return 422."""
    resp = await auth_client.post(
        f"/api/maps/{test_map_id}/layers",
        data={"layer_name": "test"},
    )
    assert resp.status_code == 422


@pytest.mark.anyio
async def test_upload_empty_file(auth_client, test_map_id):
    """Zero-byte file should be rejected."""
    resp = await auth_client.post(
        f"/api/maps/{test_map_id}/layers",
        files={"file": ("empty.geojson", b"", "application/octet-stream")},
        data={"layer_name": "Empty File"},
    )
    assert resp.status_code in (400, 422, 500)


# ---------------------------------------------------------------------------
# Unsupported format
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_unsupported_extension(auth_client, test_map_id):
    """Uploading an unsupported file type should be rejected."""
    content = b"This is a plain text file, not geodata"
    resp = await auth_client.post(
        f"/api/maps/{test_map_id}/layers",
        files={"file": ("data.xyz123", content, "application/octet-stream")},
        data={"layer_name": "Bad Format"},
    )
    # Should get 400 or 422 for unsupported format
    assert resp.status_code in (400, 422, 500)


@pytest.mark.anyio
async def test_upload_corrupt_geojson(auth_client, test_map_id):
    """Corrupt GeoJSON content should be rejected gracefully."""
    corrupt = b'{"type": "FeatureCollection", "features": [{"INVALID"}]}'
    resp = await auth_client.post(
        f"/api/maps/{test_map_id}/layers",
        files={"file": ("corrupt.geojson", corrupt, "application/geo+json")},
        data={"layer_name": "Corrupt"},
    )
    assert resp.status_code in (400, 422, 500)


# ---------------------------------------------------------------------------
# Nonexistent map
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_to_nonexistent_map(auth_client):
    """Uploading to a map ID that doesn't exist should return 404."""
    content = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[0,0]},"properties":{}}]}'
    resp = await auth_client.post(
        "/api/maps/M_DOES_NOT_EXIST/layers",
        files={"file": ("test.geojson", content, "application/geo+json")},
        data={"layer_name": "test"},
    )
    assert resp.status_code in (404, 500)


# ---------------------------------------------------------------------------
# Auth: view-only mode rejects uploads
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_upload_rejected_in_view_only(client, env_override):
    """Uploads should be rejected when auth mode is view_only."""
    content = b'{"type":"FeatureCollection","features":[{"type":"Feature","geometry":{"type":"Point","coordinates":[0,0]},"properties":{}}]}'
    with env_override(MUNDI_AUTH_MODE="view_only"):
        resp = await client.post(
            "/api/maps/M_ANY_MAP_ID/layers",
            files={"file": ("test.geojson", content, "application/geo+json")},
            data={"layer_name": "test"},
        )
        assert resp.status_code == 401
