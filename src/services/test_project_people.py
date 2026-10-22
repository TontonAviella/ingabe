"""Sharing a project: only with a colleague of the owner (someone in one of their organizations), as editor or viewer,
and a colleague can see it once shared (row-level security reads the same lists)."""

import uuid

import pytest

from src.database.pool import get_async_db_connection
from src.services import project_people

RUN_TAG = uuid.uuid4().hex[:8]


@pytest.fixture
async def team():
    """Owner and colleague in one organization, an outsider in none, and one project of the owner's."""
    ids = {k: str(uuid.uuid4()) for k in ("owner", "colleague", "outsider", "org")}
    email = {k: f"{k}-{RUN_TAG}@example.test" for k in ("owner", "colleague", "outsider")}
    project = f"P{RUN_TAG}"[:12]
    async with get_async_db_connection() as conn:
        for k in ("owner", "colleague", "outsider"):
            await conn.execute("INSERT INTO users (internal_uuid, email) VALUES ($1, $2)", ids[k], email[k])
        await conn.execute("INSERT INTO organizations (id, name, slug) VALUES ($1::uuid, 'Share Co', $2)",
                           ids["org"], f"share-{RUN_TAG}")
        for k, role in (("owner", "admin"), ("colleague", "member")):
            await conn.execute("INSERT INTO user_organizations (user_id, org_id, role) VALUES ($1, $2::uuid, $3)",
                               ids[k], ids["org"], role)
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           project, ids["owner"])
    yield {"ids": ids, "email": email, "project": project}
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", project)
        await conn.execute("DELETE FROM organizations WHERE id = $1::uuid", ids["org"])
        await conn.execute("DELETE FROM users WHERE internal_uuid = ANY($1::text[])",
                           [ids["owner"], ids["colleague"], ids["outsider"]])


async def _visible_to(user_id: str, project: str) -> bool:
    async with get_async_db_connection(user_id=user_id) as conn:
        return await conn.fetchval("SELECT count(*) FROM user_mundiai_projects WHERE id = $1", project) == 1


@pytest.mark.anyio
async def test_the_owner_shares_with_a_colleague_who_then_sees_the_project(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    assert not await _visible_to(ids["colleague"], project)
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        assert [c["email"] for c in await project_people.colleagues(conn, ids["owner"])] == [email["colleague"]]
        listed = await project_people.share(conn, project, ids["owner"], email["colleague"].upper(), "editor")
    assert listed["owner"]["email"] == email["owner"]
    assert listed["people"] == [{"user_id": ids["colleague"], "email": email["colleague"], "role": "editor"}]
    assert await _visible_to(ids["colleague"], project)


@pytest.mark.anyio
async def test_an_outsider_cannot_be_shared_with_and_learns_nothing(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        for who in (email["outsider"], f"nobody-{RUN_TAG}@example.test", email["owner"]):
            with pytest.raises(project_people.NotShareable):
                await project_people.share(conn, project, ids["owner"], who, "viewer")
        assert (await project_people.people(conn, project))["people"] == []
    assert not await _visible_to(ids["outsider"], project)


@pytest.mark.anyio
async def test_changing_a_role_replaces_it_and_unsharing_takes_the_project_away(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        await project_people.share(conn, project, ids["owner"], email["colleague"], "editor")
        listed = await project_people.share(conn, project, ids["owner"], email["colleague"], "viewer")
        assert [p["role"] for p in listed["people"]] == ["viewer"]  # one role, never both lists
        lists = await conn.fetchrow("SELECT editor_uuids, viewer_uuids FROM user_mundiai_projects WHERE id = $1", project)
        assert [str(u) for u in lists["editor_uuids"]] == [] and [str(u) for u in lists["viewer_uuids"]] == [
            ids["colleague"]]
        assert (await project_people.unshare(conn, project, ids["owner"], ids["colleague"]))["people"] == []
        assert (await project_people.unshare(conn, project, ids["owner"], ids["owner"]))["owner"]["user_id"] == ids["owner"]
    assert not await _visible_to(ids["colleague"], project)
    assert await _visible_to(ids["owner"], project)


@pytest.mark.anyio
async def test_only_the_owner_can_share_or_unshare_even_when_called_directly(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        await project_people.share(conn, project, ids["owner"], email["colleague"], "editor")
    async with get_async_db_connection(user_id=ids["colleague"]) as conn:  # an editor: RLS lets them update the row
        with pytest.raises(project_people.NotOwner):
            await project_people.share(conn, project, ids["colleague"], email["owner"], "viewer")
        with pytest.raises(project_people.NotOwner):
            await project_people.unshare(conn, project, ids["colleague"], ids["colleague"])
        assert [p["role"] for p in (await project_people.people(conn, project))["people"]] == ["editor"]
