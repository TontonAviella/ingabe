import pytest


@pytest.mark.anyio
async def test_nonexistent_endpoint(auth_client):
    response = await auth_client.get("/api/foo/bar")
    assert response.status_code == 404
