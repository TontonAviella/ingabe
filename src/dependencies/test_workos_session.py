"""The session middleware and the WebSocket handshake against a fake WorkOS (real SDK, real tokens).

The 2026-10-06 sign-outs, each as a test: two map tiles refreshing the same expired cookie
while WorkOS' clock ran ahead (one response deleting the cookie), a check that could not
finish answering 401, and the chat WebSocket spending the single-use refresh token on a
handshake that cannot send the new cookie back.
"""

from __future__ import annotations

import asyncio
from http.cookies import SimpleCookie
from types import SimpleNamespace

import httpx
import pytest
from fastapi import Depends, FastAPI, Request, WebSocketException

from src.dependencies import session as session_dep
from src.dependencies.session import WS_SESSION_REFRESH_NEEDED, verify_session_required, verify_websocket
from src.dependencies.workos_session import WorkOSSessionMiddleware
from src.services import workos_auth
from src.services._test_workos_fake import USER, fake_workos_fixture  # noqa: F401 - the fake_workos fixture

KEPT = "kept"


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(WorkOSSessionMiddleware)

    @app.get("/who")
    async def who(request: Request):  # what the middleware decided, without provisioning database rows
        ws_session = request.state.workos_session
        return {"user": ws_session.user_id if ws_session else None}

    @app.get("/private")
    async def private(_session=Depends(verify_session_required)):
        return {}

    return app


async def _get(path: str, cookie: str, times: int = 1) -> list[httpx.Response]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_app()), base_url="http://localhost:8000") as c:
        return await asyncio.gather(*[c.get(path, cookies={workos_auth.COOKIE_NAME: cookie}) for _ in range(times)])


def _cookie_sent(resp: httpx.Response) -> str | None:
    """The session cookie a response sets: its value, None when it deletes it, KEPT when untouched."""
    for header in resp.headers.get_list("set-cookie"):
        morsel = SimpleCookie(header).get(workos_auth.COOKIE_NAME)
        if morsel is not None:
            return None if morsel["max-age"] == "0" or not morsel.value.strip('"') else morsel.value
    return KEPT


class _Handshake:
    def __init__(self, cookie: str) -> None:
        self.cookies = {workos_auth.COOKIE_NAME: cookie}
        self.query_params: dict[str, str] = {}
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True


@pytest.fixture
def no_db_context(monkeypatch):
    """verify_websocket without provisioning database rows."""
    async def context(ws_session):
        return SimpleNamespace(user=ws_session.user_id)

    monkeypatch.setattr(session_dep, "workos_context", context)


@pytest.mark.anyio
async def test_tiles_sharing_an_expired_cookie_get_one_refresh_and_the_same_new_cookie(fake_workos):
    fake_workos.clock_ahead = 1.0  # as at 04:04:31: the first tile's refresh used to fail the clock check
    responses = await _get("/who", fake_workos.cookie(), times=3)
    assert [r.json()["user"] for r in responses] == [USER["id"]] * 3
    sent = {_cookie_sent(r) for r in responses}
    assert len(sent) == 1 and None not in sent and KEPT not in sent
    assert fake_workos.refresh_calls == 1


@pytest.mark.anyio
async def test_a_check_that_cannot_finish_answers_503_and_still_sends_the_new_cookie(fake_workos):
    fake_workos.clock_ahead = 600  # a token this server cannot check yet
    old = fake_workos.cookie()
    responses = await _get("/private", old, times=2)
    assert [r.status_code for r in responses] == [503, 503]
    new = {_cookie_sent(r) for r in responses}
    assert len(new) == 1 and new.pop() not in (None, KEPT, old)
    assert fake_workos.refresh_calls == 1


@pytest.mark.anyio
async def test_workos_unreachable_answers_503_and_keeps_the_cookie(fake_workos):
    """A 401 here sent the page to the sign-in screen on a network blip (2026-10-06 14:05)."""
    fake_workos.down = True
    (resp,) = await _get("/private", fake_workos.cookie())
    assert resp.status_code == 503 and _cookie_sent(resp) == KEPT


@pytest.mark.anyio
async def test_a_spent_cookie_is_signed_out_and_deleted(fake_workos):
    cookie = fake_workos.cookie()
    assert workos_auth.load(cookie) is not None  # spends its refresh token outside the middleware
    (resp,) = await _get("/private", cookie)
    assert resp.status_code == 401 and _cookie_sent(resp) is None


@pytest.mark.anyio
async def test_the_websocket_handshake_never_spends_the_refresh_token(fake_workos, no_db_context):
    """The handshake used to refresh: the browser kept the old cookie, and its next request
    carried a spent refresh token and was signed out."""
    cookie = fake_workos.cookie()
    handshake = _Handshake(cookie)
    with pytest.raises(WebSocketException) as closed:
        await verify_websocket(handshake)
    assert closed.value.code == WS_SESSION_REFRESH_NEEDED and handshake.accepted  # the page sees the code
    assert fake_workos.refresh_calls == 0
    (resp,) = await _get("/who", cookie)  # the request the page makes before reconnecting
    assert resp.json()["user"] == USER["id"] and _cookie_sent(resp) not in (None, KEPT)
    assert fake_workos.refresh_calls == 1


@pytest.mark.anyio
async def test_the_websocket_uses_a_refresh_a_page_request_just_made(fake_workos, no_db_context):
    cookie = fake_workos.cookie()
    await _get("/who", cookie)
    handshake = _Handshake(cookie)  # the browser has not applied the new cookie yet
    assert (await verify_websocket(handshake)).user == USER["id"]
    assert fake_workos.refresh_calls == 1 and not handshake.accepted  # the route accepts, not the check
    assert (await verify_websocket(_Handshake(fake_workos.cookie(expired=False)))).user == USER["id"]
