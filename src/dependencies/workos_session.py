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
session and sends the re-sealed cookie back with the response.

Refresh tokens are single-use, so several requests that arrive together with
the same expired cookie must not each refresh it: the first refresh is shared
with the others for a minute.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Optional

from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.services import workos_auth

logger = logging.getLogger(__name__)

_SHARE_SECONDS = 60
_refreshed: dict[str, tuple[float, workos_auth.WorkOSSession]] = {}
_locks: dict[str, asyncio.Lock] = {}


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


async def load_session(cookie: str) -> Optional[workos_auth.WorkOSSession]:
    """Verify a session cookie, sharing one refresh between concurrent requests."""
    key = hashlib.sha256(cookie.encode()).hexdigest()
    hit = _refreshed.get(key)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _refreshed.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        result = await asyncio.to_thread(workos_auth.load, cookie)
        if result is not None and result.refreshed_cookie:
            now = time.monotonic()
            for k in [k for k, (exp, _) in _refreshed.items() if exp <= now]:
                _refreshed.pop(k, None)
                _locks.pop(k, None)
            _refreshed[key] = (now + _SHARE_SECONDS, result)
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
        cookie_update: Optional[Response] = None
        if cookie:
            try:
                ws_session = await load_session(cookie)
            except Exception as e:  # noqa: BLE001 - WorkOS unreachable: keep the cookie, treat as signed out
                logger.warning("WorkOS session check failed: %s", e)
            else:
                if ws_session is None or ws_session.refreshed_cookie:
                    cookie_update = Response()
                    set_session_cookie(cookie_update, ws_session.refreshed_cookie if ws_session else None,
                                       is_secure(conn))
        scope.setdefault("state", {})["workos_session"] = ws_session

        if cookie_update is None:
            await self.app(scope, receive, send)
            return
        extra = [(k, v) for k, v in cookie_update.raw_headers if k == b"set-cookie"]

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, receive, send_with_cookie)
