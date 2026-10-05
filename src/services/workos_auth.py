# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""WorkOS AuthKit sign-in, the server-side way.

The browser never holds a WorkOS token. /auth/login sends the user to the
WorkOS-hosted sign-in page; /auth/callback exchanges the code here and stores
the access and refresh tokens in one sealed (encrypted) HTTP-only cookie.
Every request unseals it, verifies the access token against WorkOS' JWKS and,
when the 5-minute access token has expired, refreshes it and re-seals the
cookie. The browser-only SDK would need a paid custom auth domain (or keep the
refresh token in localStorage); this way costs nothing.

Everything WorkOS-specific lives here; ``src.dependencies.session`` turns the
result into a UserContext (user and partner ids) and provisions the rows.

Env: AUTH_PROVIDER=workos, WORKOS_API_KEY, WORKOS_CLIENT_ID,
WORKOS_COOKIE_PASSWORD (any secret of 32+ characters; encrypts the cookie).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

COOKIE_NAME = "wos_session"
COOKIE_MAX_AGE = 30 * 24 * 3600  # the refresh token, not the cookie, bounds the session


def enabled() -> bool:
    """True when WorkOS is the configured sign-in provider and its keys are set."""
    return os.environ.get("AUTH_PROVIDER", "").strip().lower() == "workos" and all(
        os.environ.get(k) for k in ("WORKOS_API_KEY", "WORKOS_CLIENT_ID", "WORKOS_COOKIE_PASSWORD")
    )


@lru_cache(maxsize=1)
def _client():
    from workos import WorkOSClient  # lazy: only loaded when WorkOS is the provider

    return WorkOSClient(api_key=os.environ["WORKOS_API_KEY"], client_id=os.environ["WORKOS_CLIENT_ID"])


def warm_up() -> None:
    """Load the SDK and check the cookie secret at startup, not on someone's first sign-in.

    Importing the WorkOS SDK takes ~7 s in the local (emulated) image; done lazily,
    the first visitor waited that long on /auth/login.
    """
    if not enabled():
        return
    _client()
    _cookie_password()


def _cookie_password() -> str:
    """The cookie key in the form the SDK needs: a Fernet key (32 bytes, url-safe base64).

    WorkOS documents the cookie password as "32+ characters", but the Python SDK
    passes it straight to Fernet, which only takes that exact encoding. A key
    already in that form is used as is (existing cookies stay valid); any other
    secret of 32+ characters is turned into one with SHA-256.
    """
    return cookie_key(os.environ["WORKOS_COOKIE_PASSWORD"])


def cookie_key(secret: str) -> str:
    secret = secret.strip()
    try:
        if len(base64.urlsafe_b64decode(secret.encode())) == 32 and len(secret) == 44:
            return secret
    except (binascii.Error, ValueError):
        pass
    if len(secret) < 32:
        raise RuntimeError("WORKOS_COOKIE_PASSWORD must be at least 32 characters")
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode()


def _value(x: Any) -> Any:
    """An enum's value, or the thing itself."""
    return getattr(x, "value", x)


def safe_return_to(value: Optional[str]) -> str:
    """Only same-site paths: an absolute or protocol-relative URL becomes "/"."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    parsed = urlparse(value)
    return "/" if parsed.scheme or parsed.netloc else value


@dataclass
class WorkOSSession:
    """What a valid session cookie says about the signed-in user."""

    user_id: str  # WorkOS user id (user_...)
    email: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    profile_picture_url: Optional[str]
    session_id: str
    organization_id: Optional[str]  # WorkOS org id (org_...)
    role: Optional[str]
    permissions: list[str] = field(default_factory=list)
    refreshed_cookie: Optional[str] = None  # set when the cookie had to be refreshed


def _from_response(resp: Any, refreshed_cookie: Optional[str] = None) -> WorkOSSession:
    user = resp.user if isinstance(resp.user, dict) else (resp.user.__dict__ if resp.user else {})
    return WorkOSSession(
        user_id=user.get("id", ""),
        email=user.get("email"),
        first_name=user.get("first_name"),
        last_name=user.get("last_name"),
        profile_picture_url=user.get("profile_picture_url"),
        session_id=resp.session_id,
        organization_id=resp.organization_id,
        role=resp.role,
        permissions=list(resp.permissions or []),
        refreshed_cookie=refreshed_cookie,
    )


def login_url(redirect_uri: str, state: str, organization_id: Optional[str] = None,
              screen_hint: Optional[str] = None) -> str:
    return _client().user_management.get_authorization_url(
        provider="authkit", redirect_uri=redirect_uri, state=state,
        organization_id=organization_id, screen_hint=screen_hint,
    )


def complete_login(code: str) -> tuple[str, dict[str, Any], Optional[str]]:
    """Exchange the callback code: (sealed cookie, WorkOS user, organization id)."""
    from workos.session import seal_session_from_auth_response  # lazy: WorkOS SDK

    resp = _client().user_management.authenticate_with_code(code=code)
    user = _user_dict(resp.user)
    sealed = seal_session_from_auth_response(
        access_token=resp.access_token, refresh_token=resp.refresh_token,
        user=user, cookie_password=_cookie_password(),
    )
    return sealed, user, resp.organization_id


def _user_dict(user: Any) -> dict[str, Any]:
    if isinstance(user, dict):
        return user
    to_dict = getattr(user, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {k: getattr(user, k, None) for k in ("id", "email", "first_name", "last_name", "profile_picture_url")}


def load(sealed: Optional[str]) -> Optional[WorkOSSession]:
    """The session in a cookie, refreshing an expired access token; None if not signed in."""
    if not sealed:
        return None
    session = _client().user_management.load_sealed_session(session_data=sealed, cookie_password=_cookie_password())
    auth = session.authenticate()
    if auth.authenticated:
        return _from_response(auth)
    if _value(getattr(auth, "reason", None)) != "invalid_jwt":  # no cookie, or one we cannot read
        return None
    refreshed = session.refresh(cookie_password=_cookie_password())
    if not refreshed.authenticated:
        reason = _value(getattr(refreshed, "reason", None))
        if reason == "refresh_network_error":  # WorkOS unreachable: not a reason to sign the user out
            raise ConnectionError("WorkOS session refresh failed: network error")
        logger.info("WorkOS session refresh denied: %s", reason)
        return None
    return _from_response(refreshed, refreshed_cookie=refreshed.sealed_session)


def switch_organization(sealed: str, organization_id: Optional[str]) -> Optional[WorkOSSession]:
    """Re-issue the session for another organization the user belongs to."""
    session = _client().user_management.load_sealed_session(session_data=sealed, cookie_password=_cookie_password())
    refreshed = session.refresh(organization_id=organization_id, cookie_password=_cookie_password())
    if not refreshed.authenticated:
        return None
    return _from_response(refreshed, refreshed_cookie=refreshed.sealed_session)


def revoke_session(session_id: str) -> None:
    """End the session at WorkOS, so the hosted page asks for sign-in again.

    Done over the API instead of WorkOS' logout redirect: that redirect only
    works once a sign-out URL is configured in the WorkOS dashboard, and without
    it WorkOS shows an error page ("app-homepage-url-not-found").
    """
    _client().user_management.revoke_session(session_id=session_id)


def organization_name(organization_id: str) -> str:
    return _client().organizations.get_organization(organization_id).name


def user_organizations(user_id: str) -> list[dict[str, Any]]:
    """The organizations a user belongs to, with their role in each."""
    page = _client().organization_membership.list_organization_memberships(user_id=user_id, limit=100)
    out = []
    for m in getattr(page, "data", page):
        if _value(getattr(m, "status", "active")) != "active":
            continue
        role = getattr(m, "role", None)
        out.append({
            "id": m.organization_id,
            "name": getattr(m, "organization_name", None) or organization_name(m.organization_id),
            "role": getattr(role, "slug", role) if role is not None else None,
        })
    return out
