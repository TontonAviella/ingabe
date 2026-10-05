"""WorkOS session handling: cookie verification, refresh, and safe redirects."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services import workos_auth


@pytest.fixture
def workos_env(monkeypatch):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32}.items():
        monkeypatch.setenv(k, v)
    client = MagicMock()
    monkeypatch.setattr(workos_auth, "_client", lambda: client)
    return client


def _ok(**kw):
    base = dict(authenticated=True, session_id="sess_1", organization_id="org_1", role="admin",
                permissions=["read"], user={"id": "user_1", "email": "a@b.rw", "first_name": "A",
                                             "last_name": "B", "profile_picture_url": None})
    return SimpleNamespace(**{**base, **kw})


def test_enabled_needs_the_provider_and_all_keys(monkeypatch):
    monkeypatch.setenv("AUTH_PROVIDER", "workos")
    monkeypatch.delenv("WORKOS_API_KEY", raising=False)
    assert not workos_auth.enabled()


@pytest.mark.parametrize("given, expected", [
    ("/project/abc", "/project/abc"), (None, "/"), ("https://evil.example/x", "/"),
    ("//evil.example", "/"), ("project", "/"),
])
def test_safe_return_to(given, expected):
    assert workos_auth.safe_return_to(given) == expected


def test_valid_cookie(workos_env):
    workos_env.user_management.load_sealed_session.return_value.authenticate.return_value = _ok()
    s = workos_auth.load("sealed")
    assert (s.user_id, s.email, s.organization_id, s.role, s.refreshed_cookie) == ("user_1", "a@b.rw", "org_1", "admin", None)


def test_expired_access_token_is_refreshed(workos_env):
    sess = workos_env.user_management.load_sealed_session.return_value
    sess.authenticate.return_value = SimpleNamespace(authenticated=False, reason=SimpleNamespace(value="invalid_jwt"))
    sess.refresh.return_value = _ok(sealed_session="new-sealed")
    assert workos_auth.load("old").refreshed_cookie == "new-sealed"


def test_refresh_denied_signs_out_but_network_error_does_not(workos_env):
    sess = workos_env.user_management.load_sealed_session.return_value
    sess.authenticate.return_value = SimpleNamespace(authenticated=False, reason="invalid_jwt")
    sess.refresh.return_value = SimpleNamespace(authenticated=False, reason="refresh_denied")
    assert workos_auth.load("old") is None
    sess.refresh.return_value = SimpleNamespace(authenticated=False, reason="refresh_network_error")
    with pytest.raises(ConnectionError):
        workos_auth.load("old")


def test_unreadable_or_missing_cookie_is_signed_out(workos_env):
    assert workos_auth.load(None) is None
    workos_env.user_management.load_sealed_session.return_value.authenticate.return_value = SimpleNamespace(
        authenticated=False, reason="invalid_session_cookie")
    assert workos_auth.load("garbage") is None


def test_user_organizations_lists_active_memberships(workos_env):
    workos_env.organization_membership.list_organization_memberships.return_value = SimpleNamespace(data=[
        SimpleNamespace(organization_id="org_1", organization_name="BK Insurance", status="active",
                        role=SimpleNamespace(slug="admin")),
        SimpleNamespace(organization_id="org_2", organization_name="Old", status="inactive", role=None),
    ])
    assert workos_auth.user_organizations("user_1") == [{"id": "org_1", "name": "BK Insurance", "role": "admin"}]


def test_create_partner_makes_the_org_once_and_invites_an_admin(monkeypatch):
    client = MagicMock()
    orgs: list = []

    def create_organization(name):
        org = SimpleNamespace(id=f"org_{len(orgs) + 1}", name=name)
        orgs.append(org)
        return org

    client.organizations.list_organizations.side_effect = lambda **kw: SimpleNamespace(data=list(orgs))
    client.organizations.create_organization.side_effect = create_organization
    client.user_management.send_invitation.side_effect = lambda **kw: SimpleNamespace(
        id="inv_1", email=kw["email"], role_slug=kw["role_slug"], expires_at="later")
    monkeypatch.setattr(workos_auth, "_client", lambda: client)

    first = workos_auth.create_partner("BK Insurance", "admin@bk.rw")
    again = workos_auth.create_partner(" bk insurance ", "second@bk.rw")
    assert first["created"] is True and again["created"] is False
    assert first["organization_id"] == again["organization_id"] == "org_1"
    assert first["invitation"]["role"] == "admin"
    assert client.organizations.create_organization.call_count == 1
