"""Routes that exist only while a feature switch is on (TOWERS, EXAMPLE_FINDER).

The switch is checked before FastAPI reads the request body or solves any dependency, so a switched-off route
answers 404 to every request, a malformed one too. A router-level dependency is not enough: FastAPI parses the body
first, so bad JSON got a 422 that showed the switched-off route exists (audit 2026-10-12).
"""

from __future__ import annotations

from typing import Any, Callable, Coroutine

from fastapi import HTTPException, Request, Response
from fastapi.routing import APIRoute


def switched_route(is_on: Callable[[], bool]) -> type[APIRoute]:
    """An APIRoute class for `APIRouter(route_class=...)` whose routes answer 404 while `is_on()` is false."""

    class SwitchedRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handler = super().get_route_handler()

            async def handle_if_on(request: Request) -> Response:
                if not is_on():
                    raise HTTPException(404, "Not Found")
                return await handler(request)

            return handle_if_on

    return SwitchedRoute
