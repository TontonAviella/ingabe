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


def test_any_long_secret_becomes_a_key_the_sdk_can_seal_with():
    """The mocked login tests never sealed a cookie; a 64-char hex secret broke every real sign-in."""
    import secrets

    from workos.session import seal_data, unseal_data

    for secret in (secrets.token_hex(32), "x" * 32):
        key = workos_auth.cookie_key(secret)
        assert unseal_data(seal_data({"user": "u1"}, key), key) == {"user": "u1"}


def test_a_key_already_in_fernet_form_is_kept_so_existing_cookies_stay_valid():
    import base64
    import os

    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    assert workos_auth.cookie_key(key) == key


def test_a_short_cookie_secret_is_refused():
    with pytest.raises(RuntimeError):
        workos_auth.cookie_key("too-short")
