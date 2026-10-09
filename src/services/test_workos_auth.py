"""WorkOS session handling: cookie verification, refresh, and safe redirects."""

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import MagicMock

import jwt
import pytest

from src.services import workos_auth
from src.services._test_workos_fake import USER, fake_workos_fixture  # noqa: F401 - the fake_workos fixture


@pytest.fixture
def workos_env(monkeypatch):
    for k, v in {"AUTH_PROVIDER": "workos", "WORKOS_API_KEY": "sk_test", "WORKOS_CLIENT_ID": "client_x",
                 "WORKOS_COOKIE_PASSWORD": "x" * 32}.items():
        monkeypatch.setenv(k, v)
    client = MagicMock()
    monkeypatch.setattr(workos_auth, "_client", lambda: client)
    return client


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


# ── Cookie checks and refresh, with the real SDK and real tokens (src.services._test_workos_fake) ──


def test_a_valid_cookie_is_read_without_calling_workos(fake_workos):
    s = workos_auth.load(fake_workos.cookie(expired=False))
    assert (s.user_id, s.email, s.organization_id, s.role, s.refreshed_cookie) == (
        USER["id"], USER["email"], "org_01TEST", "admin", None)
    assert fake_workos.refresh_calls == 0


def test_an_expired_access_token_is_refreshed(fake_workos):
    s = workos_auth.load(fake_workos.cookie())
    assert s.user_id == USER["id"] and fake_workos.refresh_calls == 1
    assert workos_auth.load(s.refreshed_cookie).refreshed_cookie is None  # the new cookie is valid as it is


@pytest.mark.parametrize("ahead", [1.0, 30.0])
def test_a_workos_clock_ahead_of_ours_does_not_sign_the_user_out(fake_workos, ahead):
    """2026-10-06: WorkOS ran 0.5-0.9 s ahead of this server; with no leeway PyJWT took each fresh
    token as "not yet valid" and 13 refreshes ended in "refresh denied: invalid_jwt"."""
    fake_workos.clock_ahead = ahead
    assert workos_auth.load(fake_workos.cookie()) is not None


def test_a_refreshed_token_this_server_cannot_check_still_keeps_the_new_cookie(fake_workos):
    """The SDK's own refresh dropped the new tokens when its check failed, with the old refresh token spent."""
    fake_workos.clock_ahead = 600  # far beyond the leeway: this server's clock is badly off
    with pytest.raises(workos_auth.SessionCheckUnavailable) as unavailable:
        workos_auth.load(fake_workos.cookie())
    kept = fake_workos.tokens_in(unavailable.value.refreshed_cookie)
    assert fake_workos.unspent(kept["refresh_token"])
    issued = re.search(r"issued \+(\d+)\.\d s", str(unavailable.value))
    assert issued and int(issued.group(1)) >= 599  # the log names the clock


def test_without_refresh_an_expired_token_is_reported_not_refreshed(fake_workos):
    with pytest.raises(workos_auth.RefreshNeeded):
        workos_auth.load(fake_workos.cookie(), refresh=False)
    assert fake_workos.refresh_calls == 0
    assert workos_auth.load(fake_workos.cookie(expired=False), refresh=False) is not None


def test_every_way_of_being_signed_out_is_logged_with_its_reason(fake_workos, monkeypatch):
    log = MagicMock()
    monkeypatch.setattr(workos_auth, "logger", log)
    cookie = fake_workos.cookie()
    assert workos_auth.load(cookie) is not None
    assert workos_auth.load(cookie) is None  # its refresh token is spent now
    assert workos_auth.load("not-a-cookie") is None
    assert workos_auth.load(None) is None
    lines = [c.args[0] % c.args[1:] for c in log.info.call_args_list]
    assert any("refresh denied" in line and "invalid_grant" in line for line in lines)
    assert "WorkOS session cookie rejected: invalid_session_cookie (12 characters)" in lines
    assert "WorkOS session: no cookie" in lines


def test_workos_unreachable_during_a_refresh_is_not_a_sign_out(fake_workos):
    cookie = fake_workos.cookie()
    fake_workos.down = True
    with pytest.raises(workos_auth.SessionCheckUnavailable) as unavailable:
        workos_auth.load(cookie)
    assert unavailable.value.refreshed_cookie is None


def test_workos_signing_keys_unreachable_is_not_a_sign_out(fake_workos, monkeypatch):
    """2026-10-06 14:05: "Network is unreachable" while fetching the keys (this fake's are not cached yet)."""
    def keys_unreachable(_self):
        raise jwt.PyJWKClientConnectionError("Network is unreachable")

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", keys_unreachable)
    with pytest.raises(workos_auth.SessionCheckUnavailable):
        workos_auth.load(fake_workos.cookie(expired=False))


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


class _Refused(Exception):
    def __init__(self, code, message="refused", status_code=400):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def test_inviting_someone_already_invited_sends_their_invitation_again(monkeypatch):
    client = MagicMock()
    client.user_management.send_invitation.side_effect = _Refused("email_already_invited_to_organization")
    client.user_management.list_invitations.return_value = SimpleNamespace(
        data=[SimpleNamespace(id="inv_9", state="pending")])
    client.user_management.resend_invitation.return_value = SimpleNamespace(
        id="inv_9", email="it@bk.rw", role_slug="admin", expires_at="later")
    monkeypatch.setattr(workos_auth, "_client", lambda: client)

    out = workos_auth.invite("org_1", "it@bk.rw", "admin", None)
    assert out["resent"] is True and out["id"] == "inv_9"
    client.user_management.resend_invitation.assert_called_once_with("inv_9")


def test_other_workos_refusals_become_readable_errors(monkeypatch):
    client = MagicMock()
    client.user_management.send_invitation.side_effect = _Refused("invalid_email", "Email is not valid.")
    monkeypatch.setattr(workos_auth, "_client", lambda: client)
    with pytest.raises(ValueError, match="Email is not valid"):
        workos_auth.invite("org_1", "nope", "member", None)


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
