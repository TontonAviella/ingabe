import pytest
import json


@pytest.mark.anyio
async def test_reject_empty_geojson_upload(auth_client):
    create_resp = await auth_client.post(
        "/api/maps/create",
        json={"title": "Empty Upload Test", "description": "Reject empty vector"},
    )
    assert create_resp.status_code == 200, create_resp.text
    map_id = create_resp.json()["id"]

    empty_fc = {"type": "FeatureCollection", "features": []}
    payload = json.dumps(empty_fc).encode("utf-8")

    resp = await auth_client.post(
        f"/api/maps/{map_id}/layers",
        files={"file": ("empty.geojson", payload, "application/geo+json")},
        data={"layer_name": "Empty"},
    )

    assert resp.status_code == 400, (
        f"Expected 400 for empty upload, got: {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert "detail" in body, body
    assert "contains no features" in body["detail"].lower()
