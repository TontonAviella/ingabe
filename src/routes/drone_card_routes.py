"""Drone question cards for a photo layer. The rules and the words live in src/services/drone_cards.py."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import get_layer
from src.dependencies.session import UserContext, verify_session_required
from src.services import drone_cards
from src.services.insurance_engine import resolve_audience
from src.structures import async_read_conn
from src.utils import get_async_s3_client, get_bucket_name

router = APIRouter()

# Distinct photos (by uploaded file) in the layer's project whose outline overlaps this layer's.
_PHOTOS_HERE_SQL = """
SELECT count(DISTINCT COALESCE(l.metadata->>'upload_etag', l.layer_id))
  FROM map_layers l
 WHERE l.type = 'raster'
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND l.layer_id IN (
       SELECT unnest(m.layers) FROM user_mundiai_maps m
        WHERE m.soft_deleted_at IS NULL
          AND m.project_id IN (SELECT m2.project_id FROM user_mundiai_maps m2 WHERE $1 = ANY(m2.layers)))
"""


async def _photo(layer: MapLayer, session: UserContext, audience: Optional[str]) -> tuple[Any, int, str]:
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "Question cards are for drone photos")
    metadata = layer.metadata_dict
    if metadata.get("cog_status") != "ready" or not metadata.get("cog_key"):
        raise HTTPException(409, "The photo is still being processed")
    s3 = await get_async_s3_client()
    cog_url = await s3.generate_presigned_url(
        "get_object", Params={"Bucket": get_bucket_name(), "Key": metadata["cog_key"]}, ExpiresIn=900)
    user_id = session.get_user_id()
    row = {"layer_id": layer.layer_id, "name": layer.name, "bounds": layer.bounds, "metadata": metadata}
    async with async_read_conn("drone_cards", user_id=user_id) as conn:
        analysis = await drone_cards.analyse_layer(conn, row, cog_url)
        if analysis is None:
            raise HTTPException(422, "Question cards need a colour (RGB) photo")
        west, south, east, north = analysis.bounds
        photos_here = await conn.fetchval(_PHOTOS_HERE_SQL, layer.layer_id, west, south, east, north)
        reader = await resolve_audience(conn, audience, user_id, session.get_org_id())
    return analysis, max(1, int(photos_here or 0)), reader


@router.get("/layer/{layer_id}/cards", operation_id="get_drone_cards")
async def get_drone_cards(
    audience: Optional[str] = None,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """The questions for a drone photo: its summary, the cards to show first, and every card by service."""
    analysis, photos_here, reader = await _photo(layer, session, audience)
    return drone_cards.build_deck(analysis, reader, photos_here)


@router.get("/layer/{layer_id}/cards/{card_id}", operation_id="get_drone_card_answer")
async def get_drone_card_answer(
    card_id: str,
    audience: Optional[str] = None,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """One card's answer: what and where, why it matters, what to do, how sure, and the outline to draw."""
    analysis, photos_here, reader = await _photo(layer, session, audience)
    try:
        return await asyncio.to_thread(drone_cards.answer_card, card_id, analysis, reader, photos_here)
    except KeyError:
        raise HTTPException(404, f"No card {card_id}") from None
