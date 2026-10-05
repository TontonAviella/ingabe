"""/api/user/report-audience: read and validate the user's report view."""

import pytest


@pytest.mark.anyio
async def test_get_report_audience_lists_the_views(auth_client):
    r = await auth_client.get("/api/user/report-audience")
    assert r.status_code == 200
    body = r.json()
    assert body["audience"] in {"farmer", "insurance", "agronomist", "scientist"}
    assert body["source"] in {"user", "partner", "default"}
    assert [o["key"] for o in body["options"]] == ["farmer", "insurance", "agronomist", "scientist"]


@pytest.mark.anyio
async def test_put_rejects_an_unknown_view(auth_client):
    r = await auth_client.put("/api/user/report-audience", json={"audience": "astronaut"})
    assert r.status_code == 400
