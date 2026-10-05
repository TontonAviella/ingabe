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
"""Sign-in with WorkOS: /auth/login, /auth/callback, /auth/logout and /api/auth/*.

The pages redirect (to the WorkOS-hosted sign-in page and back); the API
tells the app who is signed in, which organizations they belong to, and
switches between them. All WorkOS calls go through src.services.workos_auth.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from src.database.pool import get_async_db_connection
from src.dependencies.session import UserContext, provision_workos_user, verify_session_required, workos_context
from src.dependencies.workos_session import is_secure, set_session_cookie
from src.services import workos_auth

logger = logging.getLogger(__name__)

pages = APIRouter()
api = APIRouter()

_STATE_COOKIE = "wos_state"
_orgs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _require_enabled() -> None:
    if not workos_auth.enabled():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WorkOS sign-in is not enabled")


def _redirect_uri(request: Request) -> str:
    return os.environ.get("WORKOS_REDIRECT_URI") or str(request.url_for("auth_callback"))


@pages.get("/auth/login")
async def auth_login(request: Request, return_to: str = "/", organization_id: Optional[str] = None,
                     screen_hint: Optional[str] = None):
    """Send the user to the WorkOS-hosted sign-in page."""
    _require_enabled()
    nonce = secrets.token_urlsafe(16)
    target = base64.urlsafe_b64encode(workos_auth.safe_return_to(return_to).encode()).decode()
    url = await asyncio.to_thread(
        workos_auth.login_url, _redirect_uri(request), f"{nonce}.{target}", organization_id,
        screen_hint if screen_hint in ("sign-in", "sign-up") else None,
    )
    response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(_STATE_COOKIE, nonce, max_age=600, httponly=True, samesite="lax",
                        secure=is_secure(request), path="/auth/")
    return response


@pages.get("/auth/callback", name="auth_callback")
async def auth_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None,
                        error: Optional[str] = None):
    """WorkOS sends the user back here; exchange the code and set the session cookie."""
    _require_enabled()
    nonce, _, target = (state or "").partition(".")
    if error or not code or not nonce or nonce != request.cookies.get(_STATE_COOKIE):
        logger.warning("WorkOS callback rejected: error=%s code=%s state_ok=%s", error, bool(code),
                       bool(nonce) and nonce == request.cookies.get(_STATE_COOKIE))
        return RedirectResponse("/?sign_in_error=1", status_code=status.HTTP_302_FOUND)
    try:
        return_to = workos_auth.safe_return_to(base64.urlsafe_b64decode(target.encode()).decode())
    except Exception:  # noqa: BLE001 - a garbled state just lands on the home page
        return_to = "/"
    try:
        sealed, user, _org = await asyncio.to_thread(workos_auth.complete_login, code)
    except Exception as e:  # noqa: BLE001 - shown to the user as a failed sign-in, logged here
        logger.warning("WorkOS code exchange failed: %s", e)
        return RedirectResponse("/?sign_in_error=1", status_code=status.HTTP_302_FOUND)
    await provision_workos_user(user["id"], user.get("email"))  # links an existing account by email
    response = RedirectResponse(return_to, status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, sealed, is_secure(request))
    response.delete_cookie(_STATE_COOKIE, path="/auth/")
    return response


@pages.get("/auth/logout")
async def auth_logout(request: Request):
    """End the WorkOS session and clear the cookie."""
    ws_session = getattr(request.state, "workos_session", None)
    if workos_auth.enabled() and ws_session is not None:
        try:
            await asyncio.to_thread(workos_auth.revoke_session, ws_session.session_id)
        except Exception:  # noqa: BLE001 - the app cookie is cleared anyway; logged for follow-up
            logger.warning("Revoking WorkOS session %s failed", ws_session.session_id, exc_info=True)
    response = RedirectResponse("/?signed_out=1", status_code=status.HTTP_302_FOUND)
    set_session_cookie(response, None, is_secure(request))
    return response


async def _organizations(workos_user_id: str) -> list[dict[str, Any]]:
    hit = _orgs_cache.get(workos_user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    orgs = await asyncio.to_thread(workos_auth.user_organizations, workos_user_id)
    _orgs_cache[workos_user_id] = (time.monotonic() + 60, orgs)
    return orgs


async def _me(request: Request, session: UserContext) -> dict[str, Any]:
    ws_session = getattr(request.state, "workos_session", None)
    org = None
    if session.get_org_id():
        async with get_async_db_connection() as conn:
            name = await conn.fetchval("SELECT name FROM organizations WHERE id::text = $1", session.get_org_id())
        org = {"id": ws_session.organization_id if ws_session else None, "name": name, "role": session.get_org_role()}
    return {
        "provider": "workos",
        "user": {
            "email": session.get_email(),
            "first_name": ws_session.first_name if ws_session else None,
            "last_name": ws_session.last_name if ws_session else None,
            "picture": ws_session.profile_picture_url if ws_session else None,
        },
        "organization": org,
        "organizations": await _organizations(ws_session.user_id) if ws_session else [],
    }


@api.get("/me")
async def auth_me(request: Request, session: UserContext = Depends(verify_session_required)):
    """Who is signed in, the active organization, and the organizations they can switch to."""
    _require_enabled()
    return await _me(request, session)


class OrganizationSwitch(BaseModel):
    organization_id: Optional[str] = None  # None: no organization (personal)


@api.post("/organization")
async def auth_switch_organization(body: OrganizationSwitch, request: Request,
                                   session: UserContext = Depends(verify_session_required)):
    """Make another of the user's organizations the active one."""
    _require_enabled()
    cookie = request.cookies.get(workos_auth.COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sign in required")
    switched = await asyncio.to_thread(workos_auth.switch_organization, cookie, body.organization_id)
    if switched is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a member of that organization")
    request.state.workos_session = switched
    new_session = await workos_context(switched)
    response = JSONResponse(await _me(request, new_session))
    set_session_cookie(response, switched.refreshed_cookie, is_secure(request))
    return response
