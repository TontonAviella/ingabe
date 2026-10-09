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


# --- /api/user/industry ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_industry_lists_the_three_industries(auth_client):
    r = await auth_client.get("/api/user/industry")
    assert r.status_code == 200
    body = r.json()
    assert body["industry"] in {None, "agriculture", "power_grid", "telecom"}
    assert [o["key"] for o in body["options"]] == ["agriculture", "power_grid", "telecom"]
    assert all(o["label"] and o["note"] for o in body["options"])
    assert isinstance(body["can_choose"], bool)  # false without an account row: the app does not ask (R1-30)


@pytest.mark.anyio
async def test_put_industry_rejects_an_unknown_one(auth_client):
    r = await auth_client.put("/api/user/industry", json={"industry": "mining"})
    assert r.status_code == 400


@pytest.mark.anyio
async def test_an_industry_is_saved_on_the_account_and_can_be_changed():
    import uuid

    from src.database.pool import get_async_db_connection
    from src.services import industry

    user = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        try:
            assert await industry.industry_of(conn, user) is None  # not chosen yet: the app asks
            assert await industry.save_industry(conn, user, "power_grid")
            assert await industry.industry_of(conn, user) == "power_grid"
            assert await industry.save_industry(conn, user, "telecom")
            assert await industry.industry_of(conn, user) == "telecom"
            assert not await industry.save_industry(conn, str(uuid.uuid4()), "agriculture")  # no account row
        finally:
            await conn.execute("DELETE FROM users WHERE internal_uuid = $1", user)
