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


# --- A company works in one industry (2026-10-09) -------------------------------------------------------------------

@pytest.mark.anyio
async def test_a_companys_industry_is_set_by_its_admins_and_followed_by_its_members(client):
    import uuid

    from src.database.pool import get_async_db_connection
    from src.dependencies.session import WorkOSUserContext, verify_session_required
    from src.services import industry
    from src.wsgi import app

    admin, member, loner = (str(uuid.uuid4()) for _ in range(3))
    org = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        for user in (admin, member, loner):
            await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        await conn.execute("INSERT INTO organizations (id, name, slug) VALUES ($1::uuid, 'Grid Co', $2)", org, f"grid-{org[:8]}")

    def as_(user, role, org_id=org):
        app.dependency_overrides[verify_session_required] = lambda: WorkOSUserContext(user, f"w-{user[:6]}", org_id=org_id,
                                                                                      org_role=role)
    try:
        as_(member, "member")
        body = (await client.get("/api/user/industry")).json()
        assert body["industry"] is None and body["company"] == {"name": "Grid Co", "industry": None, "can_set": False}
        assert (await client.put("/api/user/industry", json={"industry": "agriculture"})).status_code == 200
        async with get_async_db_connection() as conn:  # a member's choice is their own until the company chooses
            assert await industry.company_industry(conn, org) is None

        as_(admin, "admin")
        body = (await client.put("/api/user/industry", json={"industry": "power_grid"})).json()
        assert body["industry"] == "power_grid" and body["source"] == "company" and body["company"]["can_set"]

        as_(member, "member")  # the company's choice now wins over the member's own, and they cannot override it
        body = (await client.get("/api/user/industry")).json()
        assert body["industry"] == "power_grid" and body["source"] == "company"
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 403
        async with get_async_db_connection() as conn:
            assert await industry.industry_for_new_project(conn, member, org) == "power_grid"
            assert await industry.industry_for_new_project(conn, member, None) == "agriculture"  # outside the company
            assert await industry.industry_for_new_project(conn, loner, None) == "agriculture"  # nobody chose: default
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1)", [admin, member, loner])
