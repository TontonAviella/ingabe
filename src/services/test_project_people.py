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


# --- Guests: one project, anyone by email, for a limited time -------------------------------------------------------

@pytest.fixture
async def second_project(team):
    """Another project of the owner's: a guest of the first must never see it."""
    other = f"Q{RUN_TAG}"[:12]
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           other, team["ids"]["owner"])
    yield other
    async with get_async_db_connection() as conn:
        await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", other)


async def _invite(team, who: str, role: str = "viewer", days: int = 7) -> dict:
    async with get_async_db_connection(user_id=team["ids"]["owner"]) as conn:
        return await project_people.invite_guest(conn, team["project"], team["ids"]["owner"], who, role, days)


async def _claim(user_id: str, email: str, verified: bool = True) -> list:
    async with get_async_db_connection() as conn:  # no user, as at sign-in
        return await project_people.claim_guest_invitations(conn, user_id, email, verified)


@pytest.mark.anyio
async def test_a_guest_sees_this_project_only_once_their_verified_address_signs_in(team, second_project):
    ids, email, project = team["ids"], team["email"], team["project"]
    invited = await _invite(team, email["outsider"].upper())
    assert invited["signed_in"] is False and invited["email"] == email["outsider"]
    assert not await _visible_to(ids["outsider"], project)
    assert await _claim(ids["outsider"], email["outsider"], verified=False) == []  # an unverified address gets nothing
    assert await _claim(ids["colleague"], email["colleague"]) == []  # another address: a forwarded link does nothing
    assert await _claim(ids["outsider"], email["outsider"]) == [project]
    assert await _visible_to(ids["outsider"], project)
    assert not await _visible_to(ids["outsider"], second_project)  # this project only
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        listed = await project_people.people(conn, project)
    assert listed["people"] == []  # a guest is listed as a guest, not as a colleague
    assert [(g["email"], g["role"], g["status"]) for g in listed["guests"]] == [(email["outsider"], "viewer", "active")]
    assert await _claim(ids["outsider"], email["outsider"]) == []  # the invitation works once


@pytest.mark.anyio
async def test_a_guest_loses_the_project_when_their_time_runs_out(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    await _invite(team, email["outsider"], "editor", 1)
    await _claim(ids["outsider"], email["outsider"])
    assert await _visible_to(ids["outsider"], project)
    async with get_async_db_connection() as conn:
        await conn.execute("UPDATE project_guests SET expires_at = now() - interval '1 second' WHERE project_id = $1",
                           project)
        assert await project_people.end_expired_guests(conn) == 1
        assert await project_people.end_expired_guests(conn) == 0  # once
        assert await project_people.open_guests(conn, project) == []
    assert not await _visible_to(ids["outsider"], project)


@pytest.mark.anyio
async def test_removing_a_guest_ends_their_access_at_once_and_inviting_again_renews(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    first = await _invite(team, email["outsider"], "viewer", 1)
    again = await _invite(team, email["outsider"], "editor", 30)  # renews the time and changes the role
    assert again["id"] == first["id"] and again["expires_at"] > first["expires_at"]
    await _claim(ids["outsider"], email["outsider"])
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        lists = await conn.fetchrow("SELECT editor_uuids FROM user_mundiai_projects WHERE id = $1", project)
        assert [str(u) for u in lists["editor_uuids"]] == [ids["outsider"]]
        await project_people.remove_guest(conn, project, ids["owner"], first["id"])
        with pytest.raises(LookupError):
            await project_people.remove_guest(conn, project, ids["owner"], first["id"])
    assert not await _visible_to(ids["outsider"], project)


@pytest.mark.anyio
async def test_who_can_be_a_guest_and_for_how_long(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    for who in (email["colleague"], email["owner"], "not-an-email"):
        with pytest.raises(project_people.NotGuest):
            await _invite(team, who)
    with pytest.raises(ValueError):
        await _invite(team, email["outsider"], days=3)
    with pytest.raises(project_people.NotOwner):
        async with get_async_db_connection() as conn:
            await project_people.invite_guest(conn, project, ids["colleague"], email["outsider"], "viewer", 7)
    # The same answer whether or not the address has an account: sharing reveals nobody.
    known = await _invite(team, email["outsider"])
    unknown = await _invite(team, f"nobody-{RUN_TAG}@example.test")
    assert set(known) == set(unknown) and known["signed_in"] == unknown["signed_in"] is False


@pytest.mark.anyio
async def test_sharing_with_a_guest_the_usual_way_ends_their_time_limit(team):
    ids, email, project = team["ids"], team["email"], team["project"]
    await _invite(team, email["outsider"], "viewer", 1)
    await _claim(ids["outsider"], email["outsider"])
    async with get_async_db_connection() as conn:  # the guest joins the organization later
        await conn.execute("INSERT INTO user_organizations (user_id, org_id, role) VALUES ($1, $2::uuid, 'member')",
                           ids["outsider"], ids["org"])
    async with get_async_db_connection(user_id=ids["owner"]) as conn:
        listed = await project_people.share(conn, project, ids["owner"], email["outsider"], "editor")
    assert listed["guests"] == [] and [p["email"] for p in listed["people"]] == [email["outsider"]]
    async with get_async_db_connection() as conn:
        await conn.execute("UPDATE project_guests SET expires_at = now() - interval '1 second' WHERE project_id = $1",
                           project)
        assert await project_people.end_expired_guests(conn) == 0  # nothing left to expire
    assert await _visible_to(ids["outsider"], project)


class _Session:
    def __init__(self, user_id: str):
        self._user_id = user_id

    def get_user_id(self) -> str:
        return self._user_id


@pytest.mark.anyio
async def test_a_guest_never_sees_other_peoples_sage_chats(team):
    """Roger (2026-10-26): guests must not see the Sage chats. Every chat read is the asker's own: the chat list,
    opening one (and the map's history and live replies, which use the same owner check)."""
    from fastapi import HTTPException

    from src.dependencies.conversation import get_conversation
    from src.routes.conversation_routes import list_conversations

    ids, email, project = team["ids"], team["email"], team["project"]
    async with get_async_db_connection() as conn:
        chat = await conn.fetchval("INSERT INTO conversations (project_id, owner_uuid, title) "
                                   "VALUES ($1, $2::uuid, 'Owner chat') RETURNING id", project, ids["owner"])
    try:
        await _invite(team, email["outsider"], "editor")
        await _claim(ids["outsider"], email["outsider"])
        assert await _visible_to(ids["outsider"], project)  # the guest has the project...
        guest = _Session(ids["outsider"])
        assert await list_conversations(project, guest) == []  # ...but not the owner's chats
        with pytest.raises(HTTPException) as no:
            await get_conversation(chat, guest)
        assert no.value.status_code == 404
        assert [c.id for c in await list_conversations(project, _Session(ids["owner"]))] == [chat]
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM conversations WHERE id = $1", chat)
