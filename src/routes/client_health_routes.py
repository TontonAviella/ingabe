"""Map health from people's browsers: MapLibre and deck.gl report whether the map loaded, failed, or lost its
graphics, so a map that breaks for users shows up on /metrics (frontend_map_events_total) even when every server
check is green. Only a fixed set of event names is counted; nothing else from the browser is stored."""

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field

from src.dependencies import health_metrics
from src.dependencies.session import UserContext, verify_session_required

router = APIRouter()

MAP_EVENTS = frozenset({"map_load", "map_error", "tile_error", "deck_ready", "deck_error", "webgl_lost"})


class ClientHealthEvent(BaseModel):
    event: str = Field(max_length=32)


@router.post("/client-health", status_code=204)
async def client_health(body: ClientHealthEvent, session: UserContext = Depends(verify_session_required)) -> Response:
    if body.event in MAP_EVENTS:
        await health_metrics.count("frontend_map_events_total", {"event": body.event})
    return Response(status_code=204)
