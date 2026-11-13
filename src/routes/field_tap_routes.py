"""Field outlines from taps on a drone photo. An adapter only: the grid, the tap model, the rules and the kept
outlines live in src/services/field_taps.py. Switch FIELD_TAPS (off: every route here answers 404)."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.session import UserContext, verify_session_required
from src.routes.switched_routes import switched_route
from src.services import field_taps, industry, photo_context, photo_plots
from src.structures import async_read_conn
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

router = APIRouter(route_class=switched_route(field_taps.enabled))


class TapIn(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    add: bool = Field(default=True, description="false: 'not this part'")


class TapsIn(BaseModel):
    taps: list[TapIn] = Field(min_length=1, max_length=1 + field_taps.MAX_CORRECTIONS)
    pick: Optional[int] = Field(default=None, ge=0, le=2, description="the chosen outline, needed to correct or keep")


Corner = tuple[Annotated[float, Field(allow_inf_nan=False)], Annotated[float, Field(allow_inf_nan=False)]]


class KeepIn(TapsIn):
    moved: Optional[list[Corner]] = Field(
        default=None, min_length=3, max_length=field_taps.MAX_LINE_POINTS + 1,
        description="the outline's corners as (longitude, latitude) after the reader moved its line by hand")


class MovedLineIn(BaseModel):
    first_tap: TapIn
    moved: list[Corner] = Field(min_length=3, max_length=field_taps.MAX_LINE_POINTS + 1)


class TurnDownIn(TapsIn):
    reason: str = Field(description="one of field_taps.TURN_DOWN_REASONS")


async def _photo_key(layer: MapLayer) -> str:
    """The stored photo behind a layer of a farm project. Runs after the layer access check."""
    async with async_read_conn("field_taps.industry") as conn:
        project_industry = await industry.industry_of_layer(conn, layer.layer_id)
    if not industry.available("field_taps", project_industry):
        raise HTTPException(404, "Field outlines are for farm photos.")
    metadata = layer.metadata_dict or {}
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "Field outlines work on drone photos")
    if not photo_context.is_photo_ready(metadata):
        raise HTTPException(409, "The photo is still being processed")
    return photo_plots.photo_key(metadata)


def _taps(body: TapsIn) -> list[field_taps.Tap]:
    return [field_taps.Tap(lon=t.lon, lat=t.lat, add=t.add) for t in body.taps]


@router.get("/layer/{layer_id}/field-taps", operation_id="get_field_taps")
async def get_field_taps(layer: MapLayer = Depends(get_layer),
                         session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Whether this photo is prepared for outlines, what was measured, and the outlines kept on it."""
    key = await _photo_key(layer)
    return await field_taps.state(await get_async_s3_client(), get_bucket_name(), key)


@router.post("/layer/{layer_id}/field-taps/outline", operation_id="outline_field_from_taps")
async def outline_field_from_taps(body: TapsIn, layer: MapLayer = Depends(get_layer),
                                  session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The outlines for these taps: up to three for one tap, one for a picked outline and its corrections."""
    key = await _photo_key(layer)
    try:
        found, left_out = await field_taps.outlines_and_left_out(
            await get_async_s3_client(), get_bucket_name(), key, _taps(body), body.pick)
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None
    return {"outlines": found, "left_out": left_out}


@router.post("/layer/{layer_id}/field-taps/kept", operation_id="keep_field_outline")
async def keep_field_outline(body: KeepIn, layer: MapLayer = Depends(edit_layer),
                             session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Keep the outline these taps and this pick give. The outline is computed again here, never taken from the
    browser."""
    key = await _photo_key(layer)
    if body.pick is None:
        raise HTTPException(422, "Pick one of the outlines before keeping it.")
    try:
        return await field_taps.keep(await get_async_s3_client(), get_bucket_name(), key, _taps(body), body.pick,
                                     session.get_user_id(), body.moved)
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None


@router.post("/layer/{layer_id}/field-taps/moved-line", operation_id="measure_moved_field_line")
async def measure_moved_field_line(body: MovedLineIn, layer: MapLayer = Depends(get_layer),
                                   session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The area of an outline whose line the reader moved by hand, or why it cannot be an outline."""
    await _photo_key(layer)
    tap = field_taps.Tap(lon=body.first_tap.lon, lat=body.first_tap.lat, add=True)
    try:
        return await asyncio.to_thread(field_taps.moved_line, body.moved, tap)
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None


@router.post("/layer/{layer_id}/field-taps/turned-down", operation_id="turn_down_field_outlines")
async def turn_down_field_outlines(body: TurnDownIn, layer: MapLayer = Depends(edit_layer),
                                   session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Record that these taps ended without a kept outline, so the share of taps that are kept can be counted."""
    key = await _photo_key(layer)
    try:
        return await field_taps.turn_down(await get_async_s3_client(), get_bucket_name(), key, _taps(body),
                                          body.pick, body.reason, session.get_user_id())
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/layer/{layer_id}/field-taps/kept/{outline_id}", operation_id="remove_field_outline")
async def remove_field_outline(outline_id: str, layer: MapLayer = Depends(edit_layer),
                               session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    key = await _photo_key(layer)
    if not await field_taps.remove(await get_async_s3_client(), get_bucket_name(), key, outline_id):
        raise HTTPException(404, "No such kept outline on this photo")
    return {"removed": outline_id}
