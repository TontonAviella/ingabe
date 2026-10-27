"""The home's project list: projects are created, renamed and deleted as listed, and the list shows the projects of
the industry the user works in now (Roger, 2026-10-12)."""

import contextlib
import uuid
from typing import Optional

import pytest


@pytest.mark.anyio
async def test_project_deletion(auth_client):
    """Test creating a project, deleting it, and verifying it no longer appears in user projects."""

    # 1. Create a new project
    response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Test Project for Deletion",
        },
    )
    response.raise_for_status()
    map_data = response.json()
    project_id = map_data["project_id"]
    # 2. Verify project exists in user projects list
    list_response = await auth_client.get("/api/projects/")
    list_response.raise_for_status()
    projects_data = list_response.json()
    print("foobar", list_response.text)

    project_exists = any(
        project["id"] == project_id for project in projects_data["projects"]
    )
    assert project_exists, (
        f"Created project {project_id} not found in user projects list"
    )

    # 3. Delete the project
    delete_response = await auth_client.delete(f"/api/projects/{project_id}")
    delete_response.raise_for_status()

    # 4. Verify project no longer appears in user projects list
    list_response_after = await auth_client.get("/api/projects/")
    list_response_after.raise_for_status()
    projects_data_after = list_response_after.json()
    print("foobar", list_response_after.text)

    project_exists_after = any(
        project["id"] == project_id for project in projects_data_after["projects"]
    )
    assert not project_exists_after, (
        f"Deleted project {project_id} still appears in user projects list"
    )

    # 5. Verify project appears when include_deleted=true
    list_response_with_deleted = await auth_client.get(
        "/api/projects/?include_deleted=true"
    )
    list_response_with_deleted.raise_for_status()
    projects_data_with_deleted = list_response_with_deleted.json()

    deleted_project = next(
        (
            project
            for project in projects_data_with_deleted["projects"]
            if project["id"] == project_id
        ),
        None,
    )
    assert deleted_project is not None, (
        f"Deleted project {project_id} not found when include_deleted=true"
    )
    assert deleted_project["soft_deleted_at"] is not None, (
        f"Deleted project {project_id} should have soft_deleted_at set"
    )


@pytest.mark.anyio
async def test_project_title_update(auth_client):
    response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "New Map",
        },
    )
    response.raise_for_status()
    map_data = response.json()
    project_id = map_data["project_id"]

    get_response = await auth_client.get(f"/api/projects/{project_id}")
    get_response.raise_for_status()
    project_data = get_response.json()
    assert project_data["title"] == "New Map"

    update_payload = {"title": "Updated Project Title"}
    update_response = await auth_client.post(
        f"/api/projects/{project_id}", json=update_payload
    )
    update_response.raise_for_status()
    update_data = update_response.json()

    assert update_data["updated"] is True

    get_response = await auth_client.get(f"/api/projects/{project_id}")
    get_response.raise_for_status()
    project_data = get_response.json()

    assert project_data["title"] == "Updated Project Title"

    list_response = await auth_client.get("/api/projects/")
    list_response.raise_for_status()
    projects_data = list_response.json()

    project = next(
        (p for p in projects_data["projects"] if p["id"] == project_id),
        None,
    )
    assert project is not None
    assert project["title"] == "Updated Project Title"


# --- The home lists the projects of the industry the user works in now (Roger, 2026-10-12) -------------------------

RUN_TAG = uuid.uuid4().hex[:8]


@contextlib.asynccontextmanager
async def _people(n: int, *, org_industry: Optional[str] = None, tier: str = "partner"):
    """n fresh users (no industry chosen) and a company (with `org_industry`, of `tier`: 'internal' is Ingabe's own,
    which sees every industry); everything they own is removed after."""
    from src.database.pool import get_async_db_connection

    users = [str(uuid.uuid4()) for _ in range(n)]
    org = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        for user in users:
            await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        await conn.execute("INSERT INTO organizations (id, name, slug, industry, tier) VALUES ($1::uuid, 'Home Co', $2, $3, $4)",
                           org, f"home-{RUN_TAG}-{org[:8]}", org_industry, tier)
    try:
        yield users, org
    finally:
        from src.dependencies.session import verify_session_required
        from src.wsgi import app

        app.dependency_overrides.pop(verify_session_required, None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM user_mundiai_maps WHERE owner_uuid::text = ANY($1)", users)
            await conn.execute("DELETE FROM user_mundiai_projects WHERE owner_uuid::text = ANY($1)", users)
            await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", org)
            await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1)", users)


def _signed_in(user: str, org: Optional[str] = None, role: str = "member"):
    from src.dependencies.session import WorkOSUserContext, verify_session_required
    from src.wsgi import app

    app.dependency_overrides[verify_session_required] = lambda: WorkOSUserContext(user, f"w-{user[:6]}", org_id=org,
                                                                                  org_role=role if org else None)


async def _project(owner: str, project_industry: str, *, editors=(), viewers=(), deleted: bool = False) -> str:
    from src.database.pool import get_async_db_connection

    project_id = f"P{uuid.uuid4().hex[:11]}"
    async with get_async_db_connection() as conn:
        await conn.execute(
            "INSERT INTO user_mundiai_projects (id, owner_uuid, title, industry, editor_uuids, viewer_uuids, soft_deleted_at) "
            "VALUES ($1, $2, $3, $4, $5::uuid[], $6::uuid[], CASE WHEN $7 THEN now() END)",
            project_id, owner, f"{project_industry} {RUN_TAG}", project_industry, list(editors), list(viewers), deleted)
    return project_id


async def _home(client, query: str = "") -> dict:
    response = await client.get(f"/api/projects/{query}")
    assert response.status_code == 200, response.text
    return response.json()


def _ids(body: dict) -> set[str]:
    return {p["id"] for p in body["projects"]}


@pytest.mark.anyio
async def test_home_lists_only_the_current_industrys_projects(client):
    async with _people(2) as ((me, other), _org):
        mine_farm = await _project(me, "agriculture")
        mine_mast = await _project(me, "telecom")
        shared_mast = await _project(other, "telecom", viewers=[me])  # shared with me: listed under its own industry
        shared_farm = await _project(other, "agriculture", editors=[me])
        await _project(other, "telecom")  # not shared with me: never listed
        _signed_in(me)
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 200

        body = await _home(client)
        assert _ids(body) == {mine_mast, shared_mast}
        assert body["total_items"] == 2 and body["total_pages"] == 1
        assert {p["industry"] for p in body["projects"]} == {"telecom"}
        assert (body["industry"], body["industry_label"]) == ("telecom", "Telecom Towers")
        assert body["other_industries"] == [{"industry": "agriculture", "label": "Agriculture", "count": 2}]

        # Switching changes the list (and a new map goes to the industry being looked at).
        assert (await client.put("/api/user/industry", json={"industry": "agriculture"})).status_code == 200
        made = (await client.post("/api/maps/create", json={"title": f"New farm {RUN_TAG}"})).json()
        body = await _home(client)
        assert _ids(body) == {mine_farm, shared_farm, made["project_id"]}
        assert (body["industry"], body["industry_label"], body["total_items"]) == ("agriculture", "Agriculture", 3)
        assert body["other_industries"] == [{"industry": "telecom", "label": "Telecom Towers", "count": 2}]


@pytest.mark.anyio
async def test_the_home_follows_the_users_choice_else_the_companys(client):
    from src.database.pool import get_async_db_connection
    from src.services import industry

    async with _people(1, org_industry="telecom", tier="internal") as ((me,), org):  # Ingabe's own company
        mast = await _project(me, "telecom")
        farm = await _project(me, "agriculture")

        _signed_in(me, org)  # never chose: the company's industry is the default
        body = await _home(client)
        assert _ids(body) == {mast} and body["industry"] == "telecom"
        assert [o["industry"] for o in body["other_industries"]] == ["agriculture"]
        made = (await client.post("/api/maps/create", json={"title": f"Mast {RUN_TAG}"})).json()
        assert _ids(await _home(client)) == {mast, made["project_id"]}

        _signed_in(me, org, role="admin")  # their own choice beats the company's, and leaves the company's alone
        assert (await client.put("/api/user/industry", json={"industry": "agriculture"})).status_code == 200
        body = await _home(client)
        assert _ids(body) == {farm} and body["industry"] == "agriculture"
        _signed_in(me)  # outside the company (personal workspace): still their own choice
        assert _ids(await _home(client)) == {farm}
        async with get_async_db_connection() as conn:
            assert await industry.company_industry(conn, org) == "telecom"


@pytest.mark.anyio
async def test_recently_deleted_follows_the_industry(client):
    async with _people(1) as ((me,), _org):
        live = await _project(me, "telecom")
        gone = await _project(me, "telecom", deleted=True)
        await _project(me, "agriculture", deleted=True)
        _signed_in(me)
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 200

        assert _ids(await _home(client)) == {live}
        body = await _home(client, "?include_deleted=true")
        assert _ids(body) == {live, gone} and body["total_items"] == 2
        assert next(p for p in body["projects"] if p["id"] == gone)["soft_deleted_at"] is not None
        assert body["other_industries"] == [{"industry": "agriculture", "label": "Agriculture", "count": 1}]


@pytest.mark.anyio
async def test_pages_count_only_the_shown_industry(client):
    async with _people(1) as ((me,), _org):
        first = await _project(me, "telecom")
        second = await _project(me, "telecom")
        await _project(me, "agriculture")
        _signed_in(me)
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 200

        page_1 = await _home(client, "?limit=1")
        page_2 = await _home(client, "?limit=1&page=2")
        assert (page_1["total_items"], page_1["total_pages"], len(page_1["projects"])) == (2, 2, 1)
        assert _ids(page_1) | _ids(page_2) == {first, second}


@pytest.mark.anyio
async def test_nothing_chosen_lists_every_industry(client):
    async with _people(1) as ((me,), _org):
        farm = await _project(me, "agriculture")
        mast = await _project(me, "telecom")
        _signed_in(me)

        body = await _home(client)
        assert _ids(body) == {farm, mast} and body["total_items"] == 2
        assert (body["industry"], body["industry_label"], body["other_industries"]) == (None, None, [])


@pytest.mark.anyio
async def test_a_partner_companys_people_see_only_its_industry(client):
    """A partner company with an industry: its people's home lists only that industry, never mentions the others,
    and the industry cannot be switched (Roger, 2026-10-25)."""
    async with _people(1, org_industry="agriculture") as ((me,), org):
        farm = await _project(me, "agriculture")
        await _project(me, "telecom")  # made before, elsewhere: not shown, not counted
        _signed_in(me, org)
        body = await _home(client)
        assert _ids(body) == {farm} and body["industry"] == "agriculture"
        assert body["other_industries"] == []
        assert (await client.put("/api/user/industry", json={"industry": "telecom"})).status_code == 403
        made = (await client.post("/api/maps/create", json={"title": f"Farm {RUN_TAG}"})).json()
        assert _ids(await _home(client)) == {farm, made["project_id"]}

