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


# ── Organization members (free WorkOS user management) ────────────────────

ASSIGNABLE_ROLES = ("admin", "member")  # WorkOS default roles


def _membership_dict(m: Any) -> dict[str, Any]:
    user = getattr(m, "user", None)
    user = _user_dict(user) if user is not None else {}
    role = getattr(m, "role", None)
    name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or None
    return {
        "id": m.id, "user_id": m.user_id, "email": user.get("email"), "name": name,
        "picture": user.get("profile_picture_url"), "role": getattr(role, "slug", role),
        "status": _value(getattr(m, "status", None)),
    }


def organization_members(organization_id: str) -> list[dict[str, Any]]:
    page = _client().organization_membership.list_organization_memberships(
        organization_id=organization_id, limit=100)
    members = [_membership_dict(m) for m in getattr(page, "data", page)]
    for m in members:  # the list may not include the user object
        if not m["email"]:
            user = _user_dict(_client().user_management.get_user(m["user_id"]))
            m["email"] = user.get("email")
            m["name"] = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p) or None
            m["picture"] = user.get("profile_picture_url")
    return members


def pending_invitations(organization_id: str) -> list[dict[str, Any]]:
    page = _client().user_management.list_invitations(organization_id=organization_id, limit=100)
    return [
        {"id": i.id, "email": i.email, "role": i.role_slug, "expires_at": str(i.expires_at)}
        for i in getattr(page, "data", page) if _value(i.state) == "pending"
    ]


def _refusal(e: Exception) -> Optional[str]:
    """WorkOS' own words when it refused a request (4xx), else None."""
    status_code = getattr(e, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        return getattr(e, "message", None) or str(e)
    return None


def _invitation_dict(inv: Any, resent: bool = False) -> dict[str, Any]:
    return {"id": inv.id, "email": inv.email, "role": inv.role_slug, "expires_at": str(inv.expires_at), "resent": resent}


def invite(organization_id: str, email: str, role: str, inviter_user_id: Optional[str]) -> dict[str, Any]:
    """Email an invitation to join the organization (WorkOS sends the email).

    Someone who already has a pending invitation gets it sent again (WorkOS
    refuses a second one). Any other refusal becomes a ValueError carrying
    WorkOS' message, so the page can show it.
    """
    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
    try:
        inv = _client().user_management.send_invitation(
            email=email, organization_id=organization_id, role_slug=role, inviter_user_id=inviter_user_id)
    except Exception as e:  # noqa: BLE001 - WorkOS SDK errors, sorted below
        if getattr(e, "code", None) == "email_already_invited_to_organization":
            page = _client().user_management.list_invitations(organization_id=organization_id, email=email, limit=10)
            pending = next((i for i in getattr(page, "data", page) if _value(i.state) == "pending"), None)
            if pending is not None:
                return _invitation_dict(_client().user_management.resend_invitation(pending.id), resent=True)
        message = _refusal(e)
        if message:
            raise ValueError(message) from e
        raise
    return _invitation_dict(inv)


def _membership_in(membership_id: str, organization_id: str) -> Any:
    m = _client().organization_membership.get_organization_membership(membership_id)
    if m.organization_id != organization_id:
        raise PermissionError("membership belongs to another organization")
    return m


def set_member_role(membership_id: str, organization_id: str, role: str) -> dict[str, Any]:
    from workos.organization_membership import RoleSingle  # lazy: WorkOS SDK

    if role not in ASSIGNABLE_ROLES:
        raise ValueError(f"role must be one of {', '.join(ASSIGNABLE_ROLES)}")
    _membership_in(membership_id, organization_id)
    updated = _client().organization_membership.update_organization_membership(
        membership_id, role=RoleSingle(role_slug=role))
    return _membership_dict(updated)


def remove_member(membership_id: str, organization_id: str) -> None:
    _membership_in(membership_id, organization_id)
    _client().organization_membership.delete_organization_membership(membership_id)


def revoke_invitation(invitation_id: str, organization_id: str) -> None:
    if not any(i["id"] == invitation_id for i in pending_invitations(organization_id)):
        raise PermissionError("invitation belongs to another organization or is no longer pending")
    _client().user_management.revoke_invitation(invitation_id)


def create_partner(name: str, admin_email: str) -> dict[str, Any]:
    """Create a partner organization (or reuse the one with this exact name) and invite its first admin.

    WorkOS emails the invitation; once the admin signs in they manage the rest of
    their staff on the app's members page. Nothing else needs the WorkOS dashboard.
    """
    name = name.strip()
    page = _client().organizations.list_organizations(search=name, limit=100)
    org = next((o for o in getattr(page, "data", page) if o.name.strip().lower() == name.lower()), None)
    created = org is None
    if org is None:
        org = _client().organizations.create_organization(name=name)
    invitation = invite(org.id, admin_email.strip(), "admin", None)
    return {"organization_id": org.id, "name": org.name, "created": created, "invitation": invitation}


# ---------------------------------------------------------------------------
# Companies (partner organizations), for Ingabe staff
# ---------------------------------------------------------------------------

def _platform_admins() -> list[str]:
    return [e.strip().lower() for e in os.environ.get("PLATFORM_ADMIN_EMAILS", "").split(",") if e.strip()]


def is_platform_staff(email: Optional[str]) -> bool:
    """Ingabe staff who may add companies: PLATFORM_ADMIN_EMAILS, comma-separated."""
    return bool(email) and email.strip().lower() in _platform_admins()


def is_platform_owner(email: Optional[str]) -> bool:
    """The system owner: the FIRST email in PLATFORM_ADMIN_EMAILS (sees WorkOS dashboard guidance)."""
    admins = _platform_admins()
    return bool(email) and bool(admins) and email.strip().lower() == admins[0]


def _company_status(active_members: int, invitations: list[dict[str, Any]]) -> dict[str, str]:
    """One plain-language line per company, for the Companies page."""
    if active_members:
        return {"code": "active", "text": f"Active: {active_members} {'person' if active_members == 1 else 'people'}"}
    pending = [i for i in invitations if i["state"] == "pending"]
    if pending:
        return {"code": "invited", "text": f"Invited: waiting for {pending[0]['email']} to accept"}
    if any(i["state"] == "expired" for i in invitations):
        return {"code": "expired", "text": "Invitation expired: send it again"}
    return {"code": "no_admin", "text": "No admin yet: invite one"}


def _is_workos_sample(org: Any) -> bool:
    """WorkOS' built-in "Test Organization" (staging only): it carries the reserved example.com domain.

    WorkOS refuses to rename or delete it, and no real company has that domain.
    """
    return any(getattr(d, "domain", None) == "example.com" for d in (getattr(org, "domains", None) or []))


def companies() -> list[dict[str, Any]]:
    """Every company with its people and invitations, newest first."""
    page = _client().organizations.list_organizations(limit=100)
    out = []
    for org in getattr(page, "data", page):
        if _is_workos_sample(org):
            continue
        memberships = _client().organization_membership.list_organization_memberships(organization_id=org.id, limit=100)
        active = [m for m in getattr(memberships, "data", memberships) if _value(getattr(m, "status", None)) == "active"]
        invs = _client().user_management.list_invitations(organization_id=org.id, limit=100)
        invitations = [
            {"id": i.id, "email": i.email, "state": _value(i.state), "role": i.role_slug,
             "expires_at": str(i.expires_at), "created_at": str(getattr(i, "created_at", ""))}
            for i in getattr(invs, "data", invs)
        ]
        out.append({
            "id": org.id, "name": org.name, "created_at": str(getattr(org, "created_at", "")),
            "active_members": len(active), "invitations": invitations,
            "status": _company_status(len(active), invitations),
        })
    return out


def resend_invitation(invitation_id: str) -> dict[str, Any]:
    try:
        inv = _client().user_management.resend_invitation(invitation_id)
    except Exception as e:  # noqa: BLE001 - WorkOS SDK errors: a refusal becomes a readable message
        message = _refusal(e)
        if message:
            raise ValueError(message) from e
        raise
    return {"id": inv.id, "email": inv.email, "state": _value(inv.state), "expires_at": str(inv.expires_at)}
