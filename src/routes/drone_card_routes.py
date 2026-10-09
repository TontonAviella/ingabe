"""Drone question cards for a photo layer. The rules and the words live in src/services/drone_cards.py,
the plots in src/services/drone_plots.py."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.session import UserContext, verify_session_required
from src.services import (
    background_jobs,
    drone_cards,
    drone_plots,
    drone_vision,
    farm_records,
    field_checks,
    industry,
    photo_context,
    photo_plots,
)
from src.services.insurance_engine import resolve_audience
from src.structures import async_read_conn, get_async_db_connection
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)



async def _agriculture_project(layer: MapLayer = Depends(get_layer)) -> None:
    """The cards (plots, crops, bare ground, the crop survey) are an agriculture capability: a photo in a Power Grid
    or Telecom project gets a 404 (the panel shows nothing; 409 already means "still processing, ask again").
    Runs after the layer access check (get_layer), so it never tells a stranger anything about a layer (R1-21)."""
    async with async_read_conn("drone_cards.industry") as conn:
        project_industry = await industry.industry_of_layer(conn, layer.layer_id)
    if not industry.serves("drone_cards", project_industry):
        label = industry.INDUSTRIES[project_industry]["label"] if project_industry else "not a farm project"
        raise HTTPException(404, f"No question cards here: they are for farm photos, and this project is {label}.")


router = APIRouter(dependencies=[Depends(_agriculture_project)])



def _photo_ready(layer: MapLayer) -> dict[str, Any]:
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "Question cards are for drone photos")
    metadata = layer.metadata_dict or {}
    if not photo_context.is_photo_ready(metadata):
        raise HTTPException(409, "The photo is still being processed")
    return metadata


_LONG_URL_SECONDS = 3600  # measuring spots in a few hundred plots reads the photo for minutes


async def _cog_url(s3: Any, metadata: dict[str, Any], seconds: int = 900) -> str:
    return await photo_context.cog_url(s3, get_bucket_name(), metadata, seconds)


async def _photo(layer: MapLayer, session: UserContext, audience: Optional[str], *,
                 retry_plots: bool = False, seed: int = 0) -> tuple[Any, drone_cards.Here, str]:
    metadata = _photo_ready(layer)
    context = await photo_context.load(
        await get_async_s3_client(), get_bucket_name(), layer_id=layer.layer_id, name=layer.name, bounds=layer.bounds,
        metadata=metadata, user_id=session.get_user_id(), org_id=session.get_org_id(), audience=audience,
        retry_failed=retry_plots, seed=seed)
    if context is None:
        raise HTTPException(422, "Question cards need a colour (RGB) photo")
    return context.analysis, context.here, context.reader


@router.get("/layer/{layer_id}/cards", operation_id="get_drone_cards")
async def get_drone_cards(
    audience: Optional[str] = None,
    seed: int = 0,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """The questions for a drone photo: its summary, the cards to show first, and every card by service.
    A seed other than 0 picks other wordings and a different mix of cards."""
    analysis, here, reader = await _photo(layer, session, audience, seed=seed)
    return await asyncio.to_thread(drone_cards.build_deck, analysis, reader, here)


@router.get("/layer/{layer_id}/cards/{card_id}", operation_id="get_drone_card_answer")
async def get_drone_card_answer(
    card_id: str,
    audience: Optional[str] = None,
    seed: int = 0,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """One card's answer: what and where, why it matters, what to do, how sure, and the outline to draw."""
    analysis, here, reader = await _photo(layer, session, audience, retry_plots=True, seed=seed)
    spot_plots = drone_cards.plots_to_measure_spots(card_id, here)
    if spot_plots and here.plots is not None:
        metadata = _photo_ready(layer)
        s3, bucket = await get_async_s3_client(), get_bucket_name()
        # Bare spots come from pixels alone, so every reader of this photo and plot set shares them.
        key = (f"{field_checks.plot_set_id(photo_plots.photo_key(metadata), here.plots)}|"
               f"{','.join(str(f['properties']['number']) for f in spot_plots)}")
        spots = await drone_plots.kept_spots(s3, bucket, key)
        job = drone_plots.spots_job(key)
        if spots is None and (job is None or job.state == "failed"):
            job = drone_plots.start_spots(s3, bucket, key, await _cog_url(s3, metadata, _LONG_URL_SECONDS), spot_plots)
        here = dataclasses.replace(here, spots=spots, spots_job=None if spots is not None else job)
    try:
        return await asyncio.to_thread(drone_cards.answer_card, card_id, analysis, reader, here)
    except KeyError:
        raise HTTPException(404, f"No card {card_id}") from None


class PlotSource(BaseModel):
    source: str  # "found" for the plots Ingabe finds in the photo, or the layer id of the reader's plot map


@router.put("/layer/{layer_id}/plots/source", operation_id="choose_drone_plot_source")
async def choose_drone_plot_source(
    choice: PlotSource,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, str]:
    """Use the reader's own plot map for this photo's plots, or go back to the plots Ingabe found."""
    _photo_ready(layer)
    if choice.source == "found":
        sql, args = "UPDATE map_layers SET metadata = metadata - $2::text WHERE layer_id = $1", (layer.layer_id, photo_plots.PLOT_MAP_KEY)
    else:
        if not layer.bounds:
            raise HTTPException(409, "The photo's outline is not known yet")
        async with async_read_conn("drone_plot_maps", user_id=session.get_user_id()) as conn:
            maps = await photo_plots.plot_maps(conn, layer.layer_id, list(layer.bounds))
        if choice.source not in {m.layer_id for m, _ in maps}:
            raise HTTPException(400, "Choose a map of polygons in this project that covers the photo")
        sql = ("UPDATE map_layers SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object($2::text, $3::text) "
               "WHERE layer_id = $1")
        args = (layer.layer_id, photo_plots.PLOT_MAP_KEY, choice.source)
    async with get_async_db_connection() as conn:
        await conn.execute(sql, *args)
    return {"layer_id": layer.layer_id, "source": choice.source}


@router.get("/layer/{layer_id}/plots/{number}/picture.jpg", operation_id="drone_plot_picture")
async def drone_plot_picture(
    number: int,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> Response:
    """The picture of one plot that the vision model is shown: the plot outlined in white, a few metres around it."""
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    plots = await photo_plots.current_plots(s3, get_bucket_name(), layer.layer_id, layer.bounds, metadata,
                                            session.get_user_id(), await _cog_url(s3, metadata))
    feature = next((f for f in (plots.geojson["features"] if plots else []) if f["properties"]["number"] == number), None)
    if feature is None:
        raise HTTPException(404, f"No plot {number} on this photo")
    picture = await asyncio.to_thread(drone_vision.picture_of_plot, await _cog_url(s3, metadata), feature)
    return Response(content=picture, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/layer/{layer_id}/records", operation_id="add_farm_record")
async def add_farm_record(
    file: UploadFile = File(...),
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """Read a soil lab report or harvest records (PDF, photo or spreadsheet) into the photo's project records."""
    async with async_read_conn("farm_records", user_id=session.get_user_id()) as conn:
        project_id = await photo_context.project_of(conn, layer.layer_id)
    if not project_id:
        raise HTTPException(404, "This photo is not in a project")
    content = await file.read()
    s3 = await get_async_s3_client()
    try:
        document = await farm_records.add_document(s3, get_bucket_name(), project_id, content, file.filename or "document",
                                                   file.content_type or "")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"id": document.id, "kind": document.kind, "title": document.title,
            "soil_samples": len(document.soil_samples), "harvests": len(document.harvests), "warnings": document.warnings}


class FieldCheck(BaseModel):
    crop: str


@router.post("/layer/{layer_id}/plots/{number}/check", operation_id="check_drone_plot")
async def check_drone_plot(
    number: int,
    check: FieldCheck,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """Record the crop someone found in a plot on the ground. It replaces the model's answer in every card, counts
    toward how often the model is right, and a square of the plot becomes a reference for later surveys."""
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    plots = await photo_plots.current_plots(s3, get_bucket_name(), layer.layer_id, layer.bounds, metadata,
                                            session.get_user_id(), await _cog_url(s3, metadata))
    feature = next((f for f in (plots.geojson["features"] if plots else []) if f["properties"]["number"] == number), None)
    if feature is None:
        raise HTTPException(404, f"No plot {number} on this photo")
    # The check belongs to this project (never to the photo, which identical uploads share across partners).
    async with async_read_conn("drone_check.project", user_id=session.get_user_id()) as conn:
        project_id = await photo_context.project_of(conn, layer.layer_id)
    if not project_id:
        raise HTTPException(404, "This photo is not in a project")
    try:
        checks = await field_checks.add_check(s3, get_bucket_name(), project_id,
                                              field_checks.plot_set_id(photo_plots.photo_key(metadata), plots),
                                              number, check.crop)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    if check.crop not in ("fallow_or_bare", "other", "grass_or_pasture", "woodlot"):
        square = await asyncio.to_thread(drone_vision.closeup_of_plot, await _cog_url(s3, metadata), feature)
        await drone_vision.add_reference(s3, get_bucket_name(),
                                         drone_vision.reference_scope(session.get_org_id(), session.get_user_id()),
                                         check.crop, square, "checked in the field")
    return {"number": number, "crop": check.crop, "checks": len(checks)}


_EXPORTS = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "geojson": "application/geo+json",
}


@router.get("/layer/{layer_id}/plots.{fmt}", operation_id="download_drone_plots")
async def download_drone_plots(
    fmt: str,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> Response:
    """The photo's plots with their measurements: an Excel table, a zipped Shapefile, or GeoJSON."""
    if fmt not in _EXPORTS:
        raise HTTPException(404, "Plots download as xlsx, zip (Shapefile) or geojson")
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    plots = await photo_plots.current_plots(s3, get_bucket_name(), layer.layer_id, layer.bounds, metadata,
                                            session.get_user_id(), await _cog_url(s3, metadata))
    if plots is None:
        raise HTTPException(409, "The plots of this photo are not found yet")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", layer.name or layer.layer_id).strip("_")[:60] + "_plots"
    if fmt == "xlsx":
        body = await asyncio.to_thread(drone_plots.to_xlsx, plots, layer.name or layer.layer_id)
    elif fmt == "zip":
        body = await asyncio.to_thread(drone_plots.to_shapefile_zip, plots, stem)
    else:
        body = drone_plots.to_geojson(plots)
    return Response(content=body, media_type=_EXPORTS[fmt],
                    headers={"Content-Disposition": f'attachment; filename="{stem}.{fmt}"'})
