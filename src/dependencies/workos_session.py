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
"""HTTP side of the WorkOS session: verify the cookie once per request, refresh it.

``WorkOSSessionMiddleware`` unseals and verifies the session cookie (see
src.services.workos_auth) and puts the result on ``request.state.workos_session``
for ``verify_session``. When the access token had expired it refreshes the
session and sends the re-sealed cookie back with the response. When the check
could not finish (WorkOS unreachable) it sets ``workos_session_unavailable``:
the request is refused with 503, not 401, and the cookie is kept.

Only HTTP responses refresh, because only they can carry the new cookie back
(the WebSocket handshake asks the page to make a request first, see
src.dependencies.session.verify_websocket).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional, Union

from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.services import workos_auth

logger = logging.getLogger(__name__)

_SHARE_SECONDS = 60
_Outcome = Union[Optional[workos_auth.WorkOSSession], workos_auth.SessionCheckUnavailable]
_shared: dict[str, tuple[float, _Outcome]] = {}
_locks: dict[str, asyncio.Lock] = {}
_KEEP = object()  # leave the browser's cookie as it is


def is_secure(conn: HTTPConnection) -> bool:
    return conn.url.scheme == "https" or conn.headers.get("x-forwarded-proto", "") == "https"


def set_session_cookie(response: Response, value: Optional[str], secure: bool) -> None:
    """Write (or, with None, clear) the session cookie on a response."""
    if value is None:
        response.delete_cookie(workos_auth.COOKIE_NAME, path="/")
    else:
        response.set_cookie(
            workos_auth.COOKIE_NAME, value, max_age=workos_auth.COOKIE_MAX_AGE,
            httponly=True, samesite="lax", secure=secure, path="/",
        )


def _shared_outcome(key: str) -> tuple[bool, Optional[workos_auth.WorkOSSession]]:
    """(True, outcome) while a refresh outcome for this cookie is shared, else (False, None)."""
    hit = _shared.get(key)
    if hit is None or hit[0] <= time.monotonic():
        return False, None
    outcome = hit[1]
    if isinstance(outcome, workos_auth.SessionCheckUnavailable):
        raise workos_auth.SessionCheckUnavailable(str(outcome), outcome.refreshed_cookie)
    return True, outcome


def _share(key: str, outcome: _Outcome) -> None:
    now = time.monotonic()
    for k in [k for k, (exp, _) in _shared.items() if exp <= now]:
        _shared.pop(k, None)
        _locks.pop(k, None)
    _shared[key] = (now + _SHARE_SECONDS, outcome)


async def load_session(cookie: str, refresh: bool = True) -> Optional[workos_auth.WorkOSSession]:
    """Verify a session cookie; what one refresh decided holds for every request carrying it.

    Refresh tokens are single-use. Requests that arrive together with the same expired
    cookie must not each refresh it, and a second refresh with the old cookie must not
    overrule the first (one response setting the new cookie while another deletes it).
    So for a minute every request with that cookie gets the first outcome: the new
    session, a sign-out, or "refreshed but not checkable yet" (with the new cookie).
    With ``refresh=False`` a shared outcome is still used, but an expired token raises
    workos_auth.RefreshNeeded instead of being refreshed.
    """
    key = hashlib.sha256(cookie.encode()).hexdigest()
    found, shared = _shared_outcome(key)
    if found:
        return shared
    if not refresh:
        return await asyncio.to_thread(workos_auth.load, cookie, False)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        found, shared = _shared_outcome(key)
        if found:
            return shared
        try:
            result = await asyncio.to_thread(workos_auth.load, cookie)
        except workos_auth.SessionCheckUnavailable as e:
            if e.refreshed_cookie:
                _share(key, e)
            raise
        if result is None or result.refreshed_cookie:
            _share(key, result)
    return result


class WorkOSSessionMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not workos_auth.enabled():
            await self.app(scope, receive, send)
            return
        conn = HTTPConnection(scope)
        cookie = conn.cookies.get(workos_auth.COOKIE_NAME)
        ws_session = None
        unavailable = False
        new_cookie: object = _KEEP
        if cookie:
            try:
                ws_session = await load_session(cookie)
            except Exception as e:  # noqa: BLE001 - WorkOS unreachable or the new token not checkable: refuse, never sign out
                logger.warning("WorkOS session check failed: %s", e)
                unavailable = True
                if isinstance(e, workos_auth.SessionCheckUnavailable) and e.refreshed_cookie:
                    new_cookie = e.refreshed_cookie  # the old cookie's refresh token is spent
            else:
                if ws_session is None:
                    new_cookie = None
                elif ws_session.refreshed_cookie:
                    new_cookie = ws_session.refreshed_cookie
        state = scope.setdefault("state", {})
        state["workos_session"] = ws_session
        state["workos_session_unavailable"] = unavailable

        if new_cookie is _KEEP:
            await self.app(scope, receive, send)
            return
        cookie_update = Response()
        set_session_cookie(cookie_update, new_cookie if isinstance(new_cookie, str) else None, is_secure(conn))
        extra = [(k, v) for k, v in cookie_update.raw_headers if k == b"set-cookie"]

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_cookie)
