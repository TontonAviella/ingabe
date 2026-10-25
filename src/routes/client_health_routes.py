"""Health from people's browsers.

- Map events (/client-health): MapLibre and deck.gl report whether the map loaded, failed, or lost its graphics, so a
  map that breaks for users shows up on /metrics (frontend_map_events_total) even when every server check is green.
  Only a fixed set of event names is counted.
- Errors (/client-error): a script error, an unhandled promise rejection, or a screen that crashed (React's error
  boundary) is counted (frontend_errors_total by kind) and logged as one line (logger src.browser_errors) with the
  message, the page's path, the file and line, and the top of the stack, so the Ingabe Health page and Grafana Logs
  list them. Lengths are capped; nothing else from the browser is kept."""

import logging
import time

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from src.dependencies import health_metrics
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import UserContext, verify_session_required

router = APIRouter()

MAP_EVENTS = frozenset({"map_load", "map_error", "tile_error", "deck_ready", "deck_error", "webgl_lost"})
ERROR_KINDS = ("js_error", "promise_error", "react_crash", "app_init_failed")
browser_errors = logging.getLogger("src.browser_errors")


class ClientHealthEvent(BaseModel):
    event: str = Field(max_length=32)


PER_USER_PER_MINUTE = 30  # a page sends a handful; more is a loop or someone skewing the counts


@router.post("/client-health", status_code=204)
async def client_health(body: ClientHealthEvent, session: UserContext = Depends(verify_session_required)) -> Response:
    if body.event in MAP_EVENTS and await _within_limit(session.get_user_id() if session else "anonymous"):
        await health_metrics.count("frontend_map_events_total", {"event": body.event})
    return Response(status_code=204)


class ClientError(BaseModel):
    kind: str = Field(max_length=32)
    message: str = Field(default="", max_length=500)
    page: str = Field(default="", max_length=300)  # location.pathname
    source: str = Field(default="", max_length=300)  # file:line:column
    stack: str = Field(default="", max_length=1500)  # the first frames
    build: str = Field(default="", max_length=80)  # which frontend build (its main script's name)


@router.post("/client-error", status_code=204)
async def client_error(body: ClientError, session: UserContext = Depends(verify_session_required)) -> Response:
    user_id = session.get_user_id() if session else "anonymous"
    if body.kind in ERROR_KINDS and await _within_limit(user_id, "client_error"):
        await health_metrics.count("frontend_errors_total", {"kind": body.kind})
        browser_errors.warning(
            "browser %s: %s", body.kind, body.message[:300],
            extra={"fields": {"kind": body.kind, "page": body.page, "source": body.source, "stack": body.stack,
                              "build": body.build, "user_id": user_id}},
        )
    return Response(status_code=204)


async def _within_limit(user_id: str, what: str = "client_health") -> bool:
    r = get_async_redis()
    if r is None:
        return True
    key = f"{what}:{user_id}:{int(time.time() // 60)}"
    try:
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 120)
        return n <= PER_USER_PER_MINUTE
    except Exception:  # noqa: BLE001 - the limit is a guard, not a reason to drop reports
        return True
