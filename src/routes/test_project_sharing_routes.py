"""The project people API: the owner shares and unshares; an editor it was shared with can neither share it on nor
delete it (until 2026-10-22 any editor could delete a project)."""

import pytest

from src.dependencies.session import WorkOSUserContext, verify_session_required
from src.routes import project_routes
from src.services.test_project_people import team  # noqa: F401  (the fixture: owner, colleague, outsider, project)
from src.wsgi import app


@pytest.fixture
def signed_in_as(monkeypatch, team):  # noqa: F811
    monkeypatch.setattr(project_routes, "external_auth_enabled", lambda: True)

    def sign_in(who: str):
        context = WorkOSUserContext(team["ids"][who], f"user_{who}", team["email"][who], team["ids"]["org"], "member")
        app.dependency_overrides[verify_session_required] = lambda: context

    yield sign_in
    app.dependency_overrides.pop(verify_session_required, None)


@pytest.mark.anyio
async def test_the_owner_shares_from_the_api_and_sees_who_can_be_added(client, team, signed_in_as):  # noqa: F811
    project, email = team["project"], team["email"]
    signed_in_as("owner")
    before = (await client.get(f"/api/projects/{project}/people")).json()
    assert before["can_share"] is True and before["people"] == []
    assert [c["email"] for c in before["colleagues"]] == [email["colleague"]]

    r = await client.put(f"/api/projects/{project}/people", json={"email": email["colleague"], "role": "editor"})
    assert r.status_code == 200 and [(p["email"], p["role"]) for p in r.json()["people"]] == [
        (email["colleague"], "editor")]

    r = await client.put(f"/api/projects/{project}/people", json={"email": email["outsider"], "role": "viewer"})
    assert r.status_code == 404 and "Invite them from People first" in r.json()["detail"]

    r = await client.delete(f"/api/projects/{project}/people/{team['ids']['colleague']}")
    assert r.status_code == 200 and r.json()["people"] == []


@pytest.mark.anyio
async def test_an_editor_can_neither_share_nor_delete(client, team, signed_in_as):  # noqa: F811
    project, email = team["project"], team["email"]
    signed_in_as("owner")
    await client.put(f"/api/projects/{project}/people", json={"email": email["colleague"], "role": "editor"})

    signed_in_as("colleague")
    seen = (await client.get(f"/api/projects/{project}/people")).json()
    assert seen["can_share"] is False and seen["colleagues"] == []
    r = await client.put(f"/api/projects/{project}/people", json={"email": email["owner"], "role": "editor"})
    assert r.status_code == 403
    r = await client.delete(f"/api/projects/{project}")
    assert r.status_code == 403 and "owner" in r.json()["detail"]

    signed_in_as("owner")
    assert (await client.delete(f"/api/projects/{project}")).status_code == 200
