import pytest


@pytest.mark.anyio
async def test_create_map(auth_client):
    payload = {
        "title": "Test Map API",
    }
    response = await auth_client.post(
        "/api/maps/create",
        json=payload,
    )
    if response.status_code != 200:
        print(f"Response: {response.status_code}")
        print(f"Error response: {response.text}")
        print(f"Headers: {response.headers}")
    assert response.status_code == 200
    data = response.json()
    assert "id" in data
    assert data["title"] == "Test Map API"
    assert "created_on" in data
    assert "map_link" in data


@pytest.mark.anyio
async def test_style_json_nonexistent_map(auth_client):
    response = await auth_client.get("/api/maps/foobar/style.json")
    assert response.status_code == 404
    error_data = response.json()
    assert "detail" in error_data
    assert "not found" in error_data["detail"].lower()
