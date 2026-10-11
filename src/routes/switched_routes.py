"""Routes that exist only while a feature switch is on (TOWERS, EXAMPLE_FINDER).

While its switch is off a route does not match at all, so the router answers as for a path that does not exist: 404
for every method, a malformed body or a trailing slash (no 405, 422 or 307 that would show the route exists). A
router-level dependency is not enough: it runs after routing and after the body is parsed (audit 2026-10-12). The
handler checks again, for a switch turned off between matching and handling.
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope


def switched_route(is_on: Callable[[], bool]) -> type[APIRoute]:
    """An APIRoute class for `APIRouter(route_class=...)` whose routes answer 404 while `is_on()` is false."""

    class SwitchedRoute(APIRoute):
        def matches(self, scope: Scope) -> tuple[Match, Scope]:
            if not is_on():
                return Match.NONE, {}
            return super().matches(scope)

        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handler = super().get_route_handler()

            async def handle_if_on(request: Request) -> Response:
                if not is_on():
                    raise HTTPException(404, "Not Found")
                return await handler(request)

            return handle_if_on

    return SwitchedRoute
