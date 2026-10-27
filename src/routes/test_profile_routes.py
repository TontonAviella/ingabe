"""/api/user/report-audience: read and validate the user's report view."""

import pytest


@pytest.mark.anyio
async def test_get_report_audience_lists_the_views(auth_client):
    r = await auth_client.get("/api/user/report-audience")
    assert r.status_code == 200
    body = r.json()
    from src.services import reader_roles

    keys = [o["key"] for o in body["options"]]
    assert keys == [o["id"] for o in reader_roles.options(body["industry"])]  # the roles of the industry worked in
    assert body["audience"] in keys
    assert body["source"] in {"user", "partner", "default"}


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
    assert body["industry"] in {None, "agriculture", "telecom"}
    assert [o["key"] for o in body["options"]] == ["agriculture", "telecom"]
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
            assert await industry.save_industry(conn, user, "agriculture")
            assert await industry.industry_of(conn, user) == "agriculture"
            assert await industry.save_industry(conn, user, "telecom")
            assert await industry.industry_of(conn, user) == "telecom"
            assert not await industry.save_industry(conn, str(uuid.uuid4()), "agriculture")  # no account row
        finally:
            await conn.execute("DELETE FROM users WHERE internal_uuid = $1", user)


# --- In Ingabe's own company, switching changes only the user's own view (Roger, 2026-10-12, 2026-10-25) -----------

@pytest.mark.anyio
async def test_switching_changes_only_the_users_own_view(client):
    """In Ingabe's own company (tier 'internal') everyone saves their own industry, admins too; the company's industry
    is only the default for members who never chose, and nobody is locked out of switching."""
    import uuid

    from src.database.pool import get_async_db_connection
    from src.dependencies.session import WorkOSUserContext, verify_session_required
    from src.services import industry
    from src.wsgi import app

    admin, member, newcomer = (str(uuid.uuid4()) for _ in range(3))
    org = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        for user in (admin, member, newcomer):
            await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        await conn.execute("INSERT INTO organizations (id, name, slug, tier) VALUES ($1::uuid, 'Grid Co', $2, 'internal')",
                           org, f"grid-{org[:8]}")

    def as_(user, role):
        app.dependency_overrides[verify_session_required] = lambda: WorkOSUserContext(user, f"w-{user[:6]}", org_id=org,
                                                                                      org_role=role)

    async def company_column():
        async with get_async_db_connection() as conn:
            return await conn.fetchval("SELECT industry FROM organizations WHERE id = $1::uuid", org)

    try:
        as_(member, "member")
        body = (await client.get("/api/user/industry")).json()
        assert body["industry"] is None and body["source"] is None and body["locked"] is False
        assert body["company"] == {"name": "Grid Co", "industry": None}

        as_(admin, "admin")  # an admin's switch is their own: the company's industry is not written
        body = (await client.put("/api/user/industry", json={"industry": "agriculture"})).json()
        assert body["industry"] == "agriculture" and body["source"] == "you"
        assert body["company"] == {"name": "Grid Co", "industry": None}
        assert await company_column() is None

        async with get_async_db_connection() as conn:  # the company's default, set elsewhere (not by the picker)
            assert await industry.save_company_industry(conn, org, "telecom")

        as_(newcomer, "member")  # never chose: the company's industry is their default
        body = (await client.get("/api/user/industry")).json()
        assert body["industry"] == "telecom" and body["source"] == "company"

        as_(admin, "admin")  # their own choice beats the company's default
        body = (await client.get("/api/user/industry")).json()
        assert body["industry"] == "agriculture" and body["source"] == "you"

        as_(member, "member")  # a member of a company that has chosen can switch too
        response = await client.put("/api/user/industry", json={"industry": "agriculture"})
        assert response.status_code == 200, response.text
        assert response.json()["industry"] == "agriculture" and response.json()["source"] == "you"
        assert await company_column() == "telecom"

        async with get_async_db_connection() as conn:
            assert await industry.industry_for_new_project(conn, member, org) == "agriculture"
            assert await industry.industry_for_new_project(conn, newcomer, org) == "telecom"
            assert await industry.industry_for_new_project(conn, newcomer, None) == "agriculture"  # nobody chose
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1)", [admin, member, newcomer])


# --- A partner company works in one industry (Roger, 2026-10-25) ----------------------------------------------------

@pytest.mark.anyio
async def test_a_partner_company_works_in_one_industry(client):
    """The first person of a partner company without an industry sets it for everyone; from then on its people work
    only in it: no switch, and new projects go to it whatever they chose before."""
    import uuid

    from src.database.pool import get_async_db_connection
    from src.dependencies.session import WorkOSUserContext, verify_session_required
    from src.services import industry
    from src.wsgi import app

    first, second = (str(uuid.uuid4()) for _ in range(2))
    org = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        for user in (first, second):
            await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        await conn.execute("UPDATE users SET industry = 'telecom' WHERE internal_uuid = $1", second)  # chose before
        await conn.execute("INSERT INTO organizations (id, name, slug) VALUES ($1::uuid, 'Insure Co', $2)",
                           org, f"insure-{org[:8]}")

    def as_(user):
        app.dependency_overrides[verify_session_required] = lambda: WorkOSUserContext(user, f"w-{user[:6]}", org_id=org,
                                                                                      org_role="admin")

    try:
        as_(first)  # no industry yet: the first choice is the company's
        body = (await client.get("/api/user/industry")).json()
        assert body["locked"] is False and body["industry"] is None
        body = (await client.put("/api/user/industry", json={"industry": "agriculture"})).json()
        assert body["industry"] == "agriculture" and body["locked"] is True
        assert body["company"] == {"name": "Insure Co", "industry": "agriculture"}

        as_(second)  # their earlier choice no longer counts here
        body = (await client.get("/api/user/industry")).json()
        assert (body["industry"], body["source"], body["locked"]) == ("agriculture", "company", True)
        refused = await client.put("/api/user/industry", json={"industry": "telecom"})
        assert refused.status_code == 403 and "Agriculture" in refused.json()["detail"]

        async with get_async_db_connection() as conn:
            assert await industry.industry_for_new_project(conn, second, org) == "agriculture"
            assert await industry.industry_for_new_project(conn, second, None) == "telecom"  # outside the company
            assert not await industry.claim_company_industry(conn, org, "telecom")  # claimed once only
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1)", [first, second])



@pytest.mark.anyio
async def test_only_a_first_choice_claims_a_partner_company(client):
    """A member who already chose and later switches (a tab, a shared map) never moves the company; a second person
    whose first choice matches the claimed industry is simply saved; one who lost the race is told."""
    import uuid

    from src.database.pool import get_async_db_connection
    from src.dependencies.session import WorkOSUserContext, verify_session_required
    from src.wsgi import app

    veteran, newcomer, late = (str(uuid.uuid4()) for _ in range(3))
    org = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        for user in (veteran, newcomer, late):
            await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        await conn.execute("UPDATE users SET industry = 'agriculture' WHERE internal_uuid = $1", veteran)
        await conn.execute("INSERT INTO organizations (id, name, slug) VALUES ($1::uuid, 'Open Co', $2)",
                           org, f"open-{org[:8]}")

    def as_(user):
        app.dependency_overrides[verify_session_required] = lambda: WorkOSUserContext(user, f"w-{user[:6]}", org_id=org,
                                                                                      org_role="member")

    async def company():
        async with get_async_db_connection() as conn:
            return await conn.fetchval("SELECT industry FROM organizations WHERE id = $1::uuid", org)

    try:
        as_(veteran)  # had chosen before: a switch is theirs only, the company stays unclaimed
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 200
        assert await company() is None
        as_(newcomer)  # a first choice claims it
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).json()["locked"] is True
        assert await company() == "telecom"
        as_(late)  # their first choice matches: saved, no error
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 200
        as_(late)  # a different one is refused, naming the company's
        refused = await client.put("/api/user/industry", json={"industry": "agriculture"})
        assert refused.status_code == 403 and "Telecom Towers" in refused.json()["detail"]
    finally:
        app.dependency_overrides.pop(verify_session_required, None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1)", [veteran, newcomer, late])
