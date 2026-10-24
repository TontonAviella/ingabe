"""Map health from people's browsers: MapLibre and deck.gl report whether the map loaded, failed, or lost its
graphics, so a map that breaks for users shows up on /metrics (frontend_map_events_total) even when every server
check is green. Only a fixed set of event names is counted; nothing else from the browser is stored."""

import time

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from src.dependencies import health_metrics
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import UserContext, verify_session_required

router = APIRouter()

MAP_EVENTS = frozenset({"map_load", "map_error", "tile_error", "deck_ready", "deck_error", "webgl_lost"})


class ClientHealthEvent(BaseModel):
    event: str = Field(max_length=32)


PER_USER_PER_MINUTE = 30  # a page sends a handful; more is a loop or someone skewing the counts


@router.post("/client-health", status_code=204)
async def client_health(body: ClientHealthEvent, session: UserContext = Depends(verify_session_required)) -> Response:
    if body.event in MAP_EVENTS and await _within_limit(session.get_user_id() if session else "anonymous"):
        await health_metrics.count("frontend_map_events_total", {"event": body.event})
    return Response(status_code=204)


async def _within_limit(user_id: str) -> bool:
    r = get_async_redis()
    if r is None:
        return True
    key = f"client_health:{user_id}:{int(time.time() // 60)}"
    try:
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 120)
        return n <= PER_USER_PER_MINUTE
    except Exception:  # noqa: BLE001 - the limit is a guard, not a reason to drop reports
        return True
