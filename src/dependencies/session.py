"""Sign-in for Ingabe: FastAPI dependencies that return a UserContext.

With AUTH_PROVIDER=workos the WorkOS session cookie (verified by the session
middleware, see src.services.workos_auth) decides who is signed in, and
nothing else is consulted. Without a sign-in provider, MUNDI_AUTH_MODE
selects the legacy single-user "edit" / "view_only" mode used by self-hosted
and test environments.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from abc import ABC, abstractmethod
from typing import Optional

from fastapi import HTTPException, Request, WebSocket, status
from fastapi.exceptions import WebSocketException

from src.dependencies.workos_session import load_session
from src.services import workos_auth

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# User context — abstract base + WorkOS, legacy and service implementations
# ---------------------------------------------------------------------------


class UserContext(ABC):
    @abstractmethod
    def get_user_id(self) -> str:
        """Return the internal UUID string for this user."""
        pass

    def get_email(self) -> str | None:
        """Return the user's email from the sign-in provider, or None."""
        return None

    def get_org_id(self) -> str | None:
        """Return the internal org UUID string, or None if no org context."""
        return None

    def get_org_role(self) -> str | None:
        """Return the user's role within the active org (owner/admin/member)."""
        return None


class WorkOSUserContext(UserContext):
    """User signed in with WorkOS (sealed session cookie, see src.services.workos_auth)."""

    def __init__(self, internal_uuid: str, workos_user_id: str, email: str | None = None,
                 org_id: str | None = None, org_role: str | None = None):
        self._uuid = internal_uuid
        self._workos_user_id = workos_user_id
        self._email = email
        self._org_id = org_id
        self._org_role = org_role

    def get_user_id(self) -> str:
        return self._uuid

    def get_workos_user_id(self) -> str:
        return self._workos_user_id

    def get_email(self) -> str | None:
        return self._email

    def get_org_id(self) -> str | None:
        return self._org_id

    def get_org_role(self) -> str | None:
        return self._org_role


class LegacyUserContext(UserContext):
    """Backwards-compatible single-user context for self-hosted / test."""

    _LEGACY_UUID = "00000000-0000-0000-0000-000000000000"

    def get_user_id(self) -> str:
        return self._LEGACY_UUID


# Keep old name for backwards compatibility in tests
EditOrReadOnlyUserContext = LegacyUserContext


class ServiceUserContext(UserContext):
    """Used by service-to-service callbacks where the caller has already
    been authenticated at the transport layer (HMAC on /internal/tool-call),
    and the (user_id, partner_id) pair is trusted from a verified payload.

    Carries only the IDs needed to (a) set the RLS GUCs and (b) populate
    IngabeToolCallMetaArgs for tool dispatch. No JWT, no email, no role —
    those aren't present in the Hermes payload and tools downstream are
    expected to either tolerate None or fail with a clear error.
    """

    def __init__(self, user_uuid: str, partner_id: str | None = None) -> None:
        self._uuid = user_uuid
        self._partner_id = partner_id

    def get_user_id(self) -> str:
        return self._uuid

    def get_org_id(self) -> str | None:
        return self._partner_id


# ---------------------------------------------------------------------------
# WorkOS users and organizations
# ---------------------------------------------------------------------------


def external_auth_enabled() -> bool:
    """True when users sign in with a real provider (WorkOS), not legacy edit mode."""
    return workos_auth.enabled()


# WorkOS user/org ids -> internal ids; stable for the life of the process.
_workos_user_ids: dict[str, str] = {}
_workos_org_ids: dict[str, str] = {}
_MEMBER_ROLES = {"owner", "admin", "member"}


async def provision_workos_user(workos_user_id: str, email: str | None) -> str:
    """The internal UUID for a WorkOS user, linking an existing account by email.

    Called at sign-in (when WorkOS gives us the email): a user who used the
    app under Clerk keeps their internal UUID and so their projects.
    """
    from src.structures import async_conn

    async with async_conn("workos_user_provision") as conn:
        existing = await conn.fetchval("SELECT internal_uuid FROM users WHERE workos_user_id = $1", workos_user_id)
        if existing is None and email:
            existing = await conn.fetchval(
                "UPDATE users SET workos_user_id = $1 WHERE internal_uuid = ("
                "  SELECT internal_uuid FROM users WHERE lower(email) = lower($2) AND workos_user_id IS NULL"
                "  ORDER BY created_at LIMIT 1"
                ") RETURNING internal_uuid",
                workos_user_id, email,
            )
            if existing:
                logger.info("Linked WorkOS user %s to existing account %s by email", workos_user_id, existing)
        if existing is None:
            existing = str(uuid.uuid5(uuid.NAMESPACE_URL, f"workos:{workos_user_id}"))
            await conn.execute(
                "INSERT INTO users (internal_uuid, workos_user_id, email, created_at) "
                "VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (internal_uuid) DO NOTHING",
                existing, workos_user_id, email,
            )
            logger.info("Provisioned new user workos_user_id=%s uuid=%s", workos_user_id, existing)
    _workos_user_ids[workos_user_id] = str(existing)
    return str(existing)


async def _workos_user_uuid(workos_user_id: str, email: str | None) -> str:
    cached = _workos_user_ids.get(workos_user_id)
    return cached if cached else await provision_workos_user(workos_user_id, email)


async def resolve_workos_org(workos_org_id: str, user_uuid: str, role: str | None) -> str:
    """The internal organizations.id for a WorkOS org, creating the row the first time.

    The row (and the user's membership) is made on first sight, named from
    WorkOS. (Clerk never created organization rows, so there are none to link.)
    """
    from src.structures import async_conn

    org_id = _workos_org_ids.get(workos_org_id)
    async with async_conn("workos_org_provision") as conn:
        if org_id is None:
            org_id = await conn.fetchval("SELECT id::text FROM organizations WHERE workos_org_id = $1", workos_org_id)
        if org_id is None:
            name = await asyncio.to_thread(workos_auth.organization_name, workos_org_id)
            base = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-") or "org"
            org_id = await conn.fetchval(
                "INSERT INTO organizations (name, slug, workos_org_id) VALUES ($1, $2, $3) "
                "ON CONFLICT (workos_org_id) DO UPDATE SET name = EXCLUDED.name RETURNING id::text",
                name, f"{base}-{workos_org_id[-6:].lower()}", workos_org_id,
            )
            logger.info("Provisioned organization %s (%s) -> %s", name, workos_org_id, org_id)
        await conn.execute(
            "INSERT INTO user_organizations (user_id, org_id, role) VALUES ($1, $2::uuid, $3) "
            "ON CONFLICT (user_id, org_id) DO UPDATE SET role = EXCLUDED.role",
            user_uuid, org_id, role if role in _MEMBER_ROLES else "member",
        )
    _workos_org_ids[workos_org_id] = org_id
    return org_id


async def workos_context(ws_session) -> WorkOSUserContext:
    """UserContext for a verified WorkOS session (src.services.workos_auth.WorkOSSession)."""
    user_uuid = await _workos_user_uuid(ws_session.user_id, ws_session.email)
    org_id = None
    if ws_session.organization_id:
        org_id = await resolve_workos_org(ws_session.organization_id, user_uuid, ws_session.role)
    return WorkOSUserContext(user_uuid, ws_session.user_id, ws_session.email, org_id, ws_session.role)


def verify_session(session_required: bool = True):
    async def _verify_session(request: Request = None) -> Optional[UserContext]:
        # --- WorkOS mode: the session middleware has already verified the cookie ---
        # WorkOS alone decides. A Bearer token or missing WorkOS keys must never
        # fall through to MUNDI_AUTH_MODE=edit (the shared user).
        if workos_auth.selected():
            if not workos_auth.enabled():
                logger.error("AUTH_PROVIDER=workos but WorkOS keys are missing; refusing requests")
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="Sign-in is not configured",
                )
            ws_session = getattr(request.state, "workos_session", None) if request else None
            if ws_session is not None:
                return await workos_context(ws_session)
            if session_required:
                if request is not None and getattr(request.state, "workos_session_unavailable", False):
                    # Not a sign-out: a 401 would send the page to the sign-in screen; the
                    # cookie is kept and the next request checks it again.
                    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                        detail="Could not check your sign-in just now; try again")
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
            return None

        # --- Legacy mode: no sign-in provider ---
        auth_mode = os.environ.get("MUNDI_AUTH_MODE")
        if auth_mode == "edit":
            return LegacyUserContext()
        elif auth_mode == "view_only":
            if session_required:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Authentication required",
                )
            return None

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Set AUTH_PROVIDER=workos for sign-in, or MUNDI_AUTH_MODE for legacy mode",
        )

    return _verify_session


# Convenience functions used as FastAPI Depends() across all routes
async def verify_session_required(request: Request = None) -> Optional[UserContext]:
    return await verify_session(session_required=True)(request)


async def verify_session_optional(request: Request = None) -> Optional[UserContext]:
    return await verify_session(session_required=False)(request)


async def session_user_id(request: Request = None) -> str:
    session = await verify_session_required(request)
    return session.get_user_id()


# ---------------------------------------------------------------------------
# WebSocket authentication
# ---------------------------------------------------------------------------

WS_SESSION_REFRESH_NEEDED = 4401  # the same number is in frontendts/src/components/ProjectView.tsx


async def verify_websocket(websocket: WebSocket) -> UserContext:
    """Authenticate WebSocket connections.

    WorkOS mode: the sealed session cookie comes with the handshake. The handshake
    never refreshes an expired session: it cannot send the new cookie back, and the
    refresh token is single-use, so the browser's next request carried a spent token
    and was signed out. It closes with WS_SESSION_REFRESH_NEEDED instead; the page then
    makes a normal request (which refreshes and sets the cookie) and reconnects.
    Legacy mode: allows all in edit mode, denies in view_only.
    """
    if workos_auth.selected():
        # WorkOS alone decides (see verify_session): no ?token= fallthrough.
        if not workos_auth.enabled():
            logger.error("AUTH_PROVIDER=workos but WorkOS keys are missing; refusing WebSocket")
            raise WebSocketException(code=status.WS_1011_INTERNAL_ERROR)
        cookie = websocket.cookies.get(workos_auth.COOKIE_NAME)
        if not cookie:
            logger.info("WS handshake without a WorkOS session cookie")
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        try:
            ws_session = await load_session(cookie, refresh=False)
        except workos_auth.RefreshNeeded:
            # A close code only reaches the page once the handshake is accepted
            # (a refused handshake is an HTTP 403, seen there as 1006).
            await websocket.accept()
            raise WebSocketException(code=WS_SESSION_REFRESH_NEEDED, reason="session refresh needed")
        except Exception as e:  # noqa: BLE001 - WorkOS unreachable: retry later, not "unauthorised"
            logger.warning("WS WorkOS session check failed: %s", e)
            raise WebSocketException(code=1013)
        if ws_session is None:
            raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
        return await workos_context(ws_session)

    # Legacy mode: no sign-in provider
    auth_mode = os.environ.get("MUNDI_AUTH_MODE")
    if auth_mode == "edit":
        return LegacyUserContext()
    elif auth_mode == "view_only":
        raise WebSocketException(code=status.WS_1008_POLICY_VIOLATION)
    else:
        raise WebSocketException(code=status.WS_1011_INTERNAL_ERROR)
