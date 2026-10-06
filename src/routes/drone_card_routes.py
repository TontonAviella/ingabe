"""Drone question cards for a photo layer. The rules and the words live in src/services/drone_cards.py,
the plots in src/services/drone_plots.py."""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import get_layer
from src.dependencies.session import UserContext, verify_session_required
from src.services import drone_cards, drone_plots
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

_PLOT_SEARCH_URL_SECONDS = 4 * 3600  # the plot search reads the photo for many minutes


def _photo_ready(layer: MapLayer) -> dict[str, Any]:
    if layer.type != LAYER_TYPE_RASTER:
        raise HTTPException(400, "Question cards are for drone photos")
    metadata = layer.metadata_dict or {}
    if metadata.get("cog_status") != "ready" or not metadata.get("cog_key"):
        raise HTTPException(409, "The photo is still being processed")
    return metadata


def _photo_key(layer: MapLayer, metadata: dict[str, Any]) -> str:
    """Copies of the same uploaded file share their plots."""
    return metadata.get("upload_etag") or f"{layer.layer_id}:{metadata['cog_key']}"


async def _plots(s3: Any, layer: MapLayer, metadata: dict[str, Any], *,
                 retry_failed: bool) -> tuple[Optional[drone_plots.PlotSet], Optional[drone_plots.PlotJob]]:
    """The photo's plots if found; otherwise the search is started (or retried when asked) in the background."""
    key = _photo_key(layer, metadata)
    bucket = get_bucket_name()
    plots = await drone_plots.load_plots(s3, bucket, key)
    job = drone_plots.job(key)
    if plots is None and (job is None or (retry_failed and job.state == "failed")):
        url = await s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": metadata["cog_key"]}, ExpiresIn=_PLOT_SEARCH_URL_SECONDS)
        job = drone_plots.start_finding(s3, bucket, key, url)
    return plots, (None if plots is not None else job)


async def _photo(layer: MapLayer, session: UserContext, audience: Optional[str], *,
                 retry_plots: bool = False) -> tuple[Any, drone_cards.Here, str]:
    metadata = _photo_ready(layer)
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
    plots, plot_job = await _plots(s3, layer, metadata, retry_failed=retry_plots)
    here = drone_cards.Here(photos=max(1, int(photos_here or 0)), plots=plots, plot_job=plot_job)
    return analysis, here, reader


@router.get("/layer/{layer_id}/cards", operation_id="get_drone_cards")
async def get_drone_cards(
    audience: Optional[str] = None,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """The questions for a drone photo: its summary, the cards to show first, and every card by service."""
    analysis, here, reader = await _photo(layer, session, audience)
    return drone_cards.build_deck(analysis, reader, here)


@router.get("/layer/{layer_id}/cards/{card_id}", operation_id="get_drone_card_answer")
async def get_drone_card_answer(
    card_id: str,
    audience: Optional[str] = None,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """One card's answer: what and where, why it matters, what to do, how sure, and the outline to draw."""
    analysis, here, reader = await _photo(layer, session, audience, retry_plots=True)
    try:
        return await asyncio.to_thread(drone_cards.answer_card, card_id, analysis, reader, here)
    except KeyError:
        raise HTTPException(404, f"No card {card_id}") from None


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
    plots = await drone_plots.load_plots(s3, get_bucket_name(), _photo_key(layer, metadata))
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
