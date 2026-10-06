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
from src.services import background_jobs, drone_cards, drone_plots, drone_vision, farm_records
from src.services.insurance_engine import resolve_audience
from src.structures import async_read_conn, get_async_db_connection
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

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

# Polygon layers in the layer's project that overlap its outline: the maps a reader can choose as its plots.
# Outlines Sage drew on photos (roofs, trees) are not plot maps.
_PLOT_MAPS_SQL = """
SELECT l.layer_id, l.name, l.feature_count, l.last_edited
  FROM map_layers l
 WHERE l.type IN ('vector', 'postgis')
   AND l.geometry_type ILIKE '%polygon%'
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND COALESCE(l.metadata->>'source', '') <> 'sage_raster_object_candidates'
   AND l.layer_id IN (
       SELECT unnest(m.layers) FROM user_mundiai_maps m
        WHERE m.soft_deleted_at IS NULL
          AND m.project_id IN (SELECT m2.project_id FROM user_mundiai_maps m2 WHERE $1 = ANY(m2.layers)))
 ORDER BY l.last_edited DESC NULLS LAST
"""

_PROJECT_SQL = """
SELECT m.project_id FROM user_mundiai_maps m
 WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL
 ORDER BY m.last_edited DESC NULLS LAST LIMIT 1
"""

_PLOT_SEARCH_URL_SECONDS = 4 * 3600  # the plot search reads the photo for many minutes
_PLOT_MAP_KEY = "plot_map_layer_id"  # in the photo layer's metadata: the reader's chosen plot map


def _photo_ready(layer: MapLayer) -> dict[str, Any]:
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "Question cards are for drone photos")
    metadata = layer.metadata_dict or {}
    if metadata.get("cog_status") != "ready" or not metadata.get("cog_key"):
        raise HTTPException(409, "The photo is still being processed")
    return metadata


def _photo_key(metadata: dict[str, Any]) -> str:
    """Copies of a photo layer point at the same stored image, so they share its plots."""
    return metadata["cog_key"]


async def _plots(s3: Any, layer: MapLayer, metadata: dict[str, Any], *,
                 retry_failed: bool) -> tuple[Optional[drone_plots.PlotSet], Optional[background_jobs.Job]]:
    """The photo's plots if found; otherwise the search is started (or retried when asked) in the background."""
    key = _photo_key(metadata)
    bucket = get_bucket_name()
    plots = await drone_plots.load_plots(s3, bucket, key)
    job = drone_plots.job(key)
    if plots is None and (job is None or (retry_failed and job.state == "failed")):
        url = await s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": metadata["cog_key"]}, ExpiresIn=_PLOT_SEARCH_URL_SECONDS)
        job = drone_plots.start_finding(s3, bucket, key, url)
    return plots, (None if plots is not None else job)


async def _map_plots(user_id: str, map_layer_id: str) -> list[drone_plots.MapPlot]:
    async with async_read_conn("drone_plot_map", user_id=user_id) as conn:
        row = await conn.fetchrow("SELECT * FROM map_layers WHERE layer_id = $1", map_layer_id)
    if row is None:
        raise ValueError("the map layer is gone")
    async with await MapLayer(**dict(row)).get_ogr_source() as source:
        return await asyncio.to_thread(drone_plots.read_plot_map, source)


async def _plot_maps(conn: Any, layer_id: str, bounds: list[float]) -> list[tuple[drone_plots.PlotMap, str]]:
    """(map, version) for each polygon layer the reader can choose; the version changes when the map is edited."""
    west, south, east, north = bounds
    rows = await conn.fetch(_PLOT_MAPS_SQL, layer_id, west, south, east, north)
    return [(drone_plots.PlotMap(layer_id=r["layer_id"], name=r["name"], shapes=r["feature_count"]),
             f"{r['layer_id']}:{r['last_edited'].isoformat() if r['last_edited'] else ''}") for r in rows]


async def _plots_from_chosen_map(
    s3: Any, layer: MapLayer, metadata: dict[str, Any], user_id: str, cog_url: str,
    maps: list[tuple[drone_plots.PlotMap, str]],
) -> tuple[Optional[drone_plots.PlotSet], Optional[str], Optional[str]]:
    """(plots, chosen map layer id, error) for the plot map chosen for this photo; all None when none is chosen."""
    chosen = next(((m, version) for m, version in maps if m.layer_id == metadata.get(_PLOT_MAP_KEY)), None)
    if chosen is None:
        return None, None, None
    plot_map, version = chosen
    try:
        plots = await drone_plots.load_map_plots(
            s3, get_bucket_name(), _photo_key(metadata), version, cog_url, plot_map.name,
            lambda: _map_plots(user_id, plot_map.layer_id))
    except Exception as exc:  # the cards say the map could not be read and use the plots Ingabe found
        logger.exception("plot map %s could not be read for %s", plot_map.layer_id, layer.layer_id)
        return None, None, f"{plot_map.name} could not be read ({str(exc)[:120]})"
    return plots, plot_map.layer_id, None


async def _survey(s3: Any, metadata: dict[str, Any], plots: drone_plots.PlotSet, place: Optional[str], *,
                  retry_failed: bool) -> tuple[Optional[drone_vision.Survey], Optional[background_jobs.Job]]:
    """The vision model's look at each plot if done; otherwise it is started (or retried when asked)."""
    bucket = get_bucket_name()
    key = drone_vision.survey_key(_photo_key(metadata), plots)
    survey = await drone_vision.load_survey(s3, bucket, key)
    job = drone_vision.job(key)
    if survey is None and (job is None or (retry_failed and job.state == "failed")):
        url = await s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": metadata["cog_key"]}, ExpiresIn=_PLOT_SEARCH_URL_SECONDS)
        job = drone_vision.start_survey(s3, bucket, key, url, plots, place)
    return survey, (None if survey is not None else job)


async def _cog_url(s3: Any, metadata: dict[str, Any]) -> str:
    return await s3.generate_presigned_url(
        "get_object", Params={"Bucket": get_bucket_name(), "Key": metadata["cog_key"]}, ExpiresIn=900)


async def _photo(layer: MapLayer, session: UserContext, audience: Optional[str], *,
                 retry_plots: bool = False, seed: int = 0) -> tuple[Any, drone_cards.Here, str]:
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    cog_url = await _cog_url(s3, metadata)
    user_id = session.get_user_id()
    row = {"layer_id": layer.layer_id, "name": layer.name, "bounds": layer.bounds, "metadata": metadata}
    async with async_read_conn("drone_cards", user_id=user_id) as conn:
        analysis = await drone_cards.analyse_layer(conn, row, cog_url)
        if analysis is None:
            raise HTTPException(422, "Question cards need a colour (RGB) photo")
        west, south, east, north = analysis.bounds
        photos_here = await conn.fetchval(_PHOTOS_HERE_SQL, layer.layer_id, west, south, east, north)
        reader = await resolve_audience(conn, audience, user_id, session.get_org_id())
        maps = await _plot_maps(conn, layer.layer_id, analysis.bounds)
        project_id = await conn.fetchval(_PROJECT_SQL, layer.layer_id)
    records = await farm_records.load_records(s3, get_bucket_name(), project_id) if project_id else []
    plots, plot_map, map_error = await _plots_from_chosen_map(s3, layer, metadata, user_id, cog_url, maps)
    plot_job = None
    if plots is None:
        plots, plot_job = await _plots(s3, layer, metadata, retry_failed=retry_plots)
    survey, survey_job = None, None
    if plots is not None:
        survey, survey_job = await _survey(s3, metadata, plots, analysis.look.place, retry_failed=retry_plots)
    here = drone_cards.Here(photos=max(1, int(photos_here or 0)), plots=plots, plot_job=plot_job,
                            plot_maps=tuple(m for m, _ in maps), plot_map=plot_map, plot_map_error=map_error,
                            survey=survey, survey_job=survey_job, seed=seed, records=tuple(records))
    return analysis, here, reader


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
        s3 = await get_async_s3_client()
        key = f"{drone_vision.survey_key(_photo_key(metadata), here.plots)}|{','.join(str(f['properties']['number']) for f in spot_plots)}"
        spots = await drone_plots.load_spots(s3, get_bucket_name(), key, await _cog_url(s3, metadata), spot_plots)
        here = dataclasses.replace(here, spots=spots)
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
        sql, args = "UPDATE map_layers SET metadata = metadata - $2::text WHERE layer_id = $1", (layer.layer_id, _PLOT_MAP_KEY)
    else:
        if not layer.bounds:
            raise HTTPException(409, "The photo's outline is not known yet")
        async with async_read_conn("drone_plot_maps", user_id=session.get_user_id()) as conn:
            maps = await _plot_maps(conn, layer.layer_id, list(layer.bounds))
        if choice.source not in {m.layer_id for m, _ in maps}:
            raise HTTPException(400, "Choose a map of polygons in this project that covers the photo")
        sql = ("UPDATE map_layers SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object($2::text, $3::text) "
               "WHERE layer_id = $1")
        args = (layer.layer_id, _PLOT_MAP_KEY, choice.source)
    async with get_async_db_connection() as conn:
        await conn.execute(sql, *args)
    return {"layer_id": layer.layer_id, "source": choice.source}


async def _current_plots(s3: Any, layer: MapLayer, metadata: dict[str, Any],
                         session: UserContext) -> Optional[drone_plots.PlotSet]:
    """The photo's plots as the cards use them: from the chosen plot map, else those Ingabe found (None if not yet)."""
    if metadata.get(_PLOT_MAP_KEY) and layer.bounds:
        user_id = session.get_user_id()
        async with async_read_conn("drone_plot_maps", user_id=user_id) as conn:
            maps = await _plot_maps(conn, layer.layer_id, list(layer.bounds))
        plots, _, _ = await _plots_from_chosen_map(s3, layer, metadata, user_id, await _cog_url(s3, metadata), maps)
        if plots is not None:
            return plots
    return await drone_plots.load_plots(s3, get_bucket_name(), _photo_key(metadata))


@router.get("/layer/{layer_id}/plots/{number}/picture.jpg", operation_id="drone_plot_picture")
async def drone_plot_picture(
    number: int,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> Response:
    """The picture of one plot that the vision model is shown: the plot outlined in white, a few metres around it."""
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    plots = await _current_plots(s3, layer, metadata, session)
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
        project_id = await conn.fetchval(_PROJECT_SQL, layer.layer_id)
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
    plots = await _current_plots(s3, layer, metadata, session)
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
