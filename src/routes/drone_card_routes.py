"""Drone question cards for a photo layer. The rules and the words live in src/services/drone_cards.py,
the plots in src/services/drone_plots.py."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import logging
import os
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Optional

import rasterio
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.redis_client import get_async_redis
from src.dependencies.session import UserContext, verify_session_required
from src.services import (
    band_names,
    asked_questions,
    background_jobs,
    card_memory,
    close_look,
    crop_calendar,
    drone_cards,
    drone_plots,
    drone_vision,
    farm_records,
    field_checks,
    industry,
    photo_context,
    photo_plots,
    reader_roles,
    record_links,
    satellite_view,
)
from src.services.insurance_engine import resolve_audience
from src.structures import async_read_conn, get_async_db_connection
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS
from src.tile_cache import tile_cache
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

logger = logging.getLogger(__name__)



async def _agriculture_project(layer: MapLayer = Depends(get_layer)) -> None:
    """The cards (plots, crops, bare ground, the crop survey) are an agriculture capability: a photo in a Telecom
    project gets a 404 (the panel shows nothing; 409 already means "still processing, ask again").
    Runs after the layer access check (get_layer), so it never tells a stranger anything about a layer (R1-21)."""
    async with async_read_conn("drone_cards.industry") as conn:
        project_industry = await industry.industry_of_layer(conn, layer.layer_id)
    if not industry.available("drone_cards", project_industry):
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
        raise HTTPException(422, "Question cards need a colour photo, or a multispectral photo whose file names its bands")
    return context.analysis, context.here, context.reader


def _warm_satellite_view(layer: MapLayer) -> None:
    """The reader has this photo's questions open: start making its satellite view (plants and soil moisture) now,
    so it is ready when they ask about their crops. It took 40 seconds to 2 minutes of remote reads in the local
    containers the first time (2026-11-23) and takes nothing while fresh (satellite_view.FRESH_HOURS); a failure is logged by the job and the
    view is made when asked for instead."""
    bounds = [float(v) for v in layer.bounds] if layer.bounds else None
    if bounds is None or satellite_view.fresh(bounds):
        return

    async def work(progress: Any) -> None:
        await asyncio.to_thread(satellite_view.view, bounds, date.today())

    background_jobs.start(f"satellite-view:{layer.layer_id}", work)


@router.get("/layer/{layer_id}/cards", operation_id="get_drone_cards")
async def get_drone_cards(
    audience: Optional[str] = None,
    seed: int = 0,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """The questions for a drone photo: its summary, the cards to show first, and every card by service, worded for
    the reader's role. Seed 0 gives the day's set (the same all day, new tomorrow); another seed ("Other questions")
    another wording and mix. The season's moment, what changed since the reader last looked and what they opened
    today order the cards (src/services/card_memory.py)."""
    reader_id = session.get_user_id() or "anonymous"
    seed = seed or card_memory.daily_seed(reader_id, layer.layer_id)
    analysis, here, reader = await _photo(layer, session, audience, seed=seed)
    redis = get_async_redis()
    seen, before = await card_memory.recall(redis, reader_id, layer.layer_id)
    now = await asyncio.to_thread(drone_cards.statuses, analysis, here)
    here = dataclasses.replace(here, moment=crop_calendar.season_moment(), seen=seen,
                               changed=card_memory.changed(before, now))
    deck = await asyncio.to_thread(drone_cards.build_deck, analysis, reader, here)
    await card_memory.remember_deck(redis, reader_id, layer.layer_id, now)
    _warm_satellite_view(layer)
    return deck


@router.get("/layer/{layer_id}/cards/{card_id}", operation_id="get_drone_card_answer")
async def get_drone_card_answer(
    card_id: str,
    audience: Optional[str] = None,
    seed: int = 0,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """One card's answer: what and where, why it matters, what to do, how sure, and the outline to draw."""
    reader_id = session.get_user_id() or "anonymous"
    seed = seed or card_memory.daily_seed(reader_id, layer.layer_id)  # the same wording as the day's deck
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
        answer = await asyncio.to_thread(drone_cards.answer_card, card_id, analysis, reader, here)
    except KeyError:
        raise HTTPException(404, f"No card {card_id}") from None
    await card_memory.remember_opened(get_async_redis(), reader_id, layer.layer_id, card_id, answer.get("status"))
    listed = drone_cards.documents(card_id, analysis, here)
    if listed:  # only for someone who may remove a document; a viewer gets no file names and no Remove
        try:
            await _records_project(layer.layer_id, session.get_user_id())
            answer["documents"] = listed
        except HTTPException:
            pass
    return answer


async def _records_project(layer_id: str, user_id: str) -> str:
    """The project whose records this photo reads, for someone who owns or edits that very project (edit_layer
    passes for an editor of any project the layer is in); 404 or 403 otherwise. A deleted project is no one's."""
    async with async_read_conn("farm_records", user_id=user_id) as conn:
        project_id = await photo_context.project_of(conn, layer_id)
        if not project_id:
            raise HTTPException(404, "This photo is not in a project")
        row = await conn.fetchrow("SELECT owner_uuid, editor_uuids FROM user_mundiai_projects "
                                  "WHERE id = $1 AND soft_deleted_at IS NULL", project_id)
    editors = [str(u) for u in (row["editor_uuids"] or [])] if row else []
    if row is None or user_id not in [str(row["owner_uuid"]), *editors]:
        raise HTTPException(403, "You do not have edit access to this farm's records")
    return project_id


class AskedQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=asked_questions.QUESTION_CHARS)


@router.post("/layer/{layer_id}/asked", status_code=202, operation_id="record_drone_photo_question")
async def record_drone_photo_question(
    body: AskedQuestion,
    background: BackgroundTasks,
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """A question the reader asked Sage about this photo, kept to learn what people ask (asked_questions): read by
    the model after the reply is sent."""
    user_id, org_id = session.get_user_id(), session.get_org_id()
    async with async_read_conn("drone_cards.asked_role", user_id=user_id) as conn:
        role = await reader_roles.role_in(conn, "agriculture", None, user_id, org_id)
    asked_id = await asked_questions.record(user_id=user_id, org_id=org_id, industry="agriculture", role=role,
                                            layer_id=layer.layer_id, question=body.question)
    if asked_id is not None:
        background.add_task(asked_questions.learn, asked_id)
    return {"kept": asked_id is not None}


@router.get("/layer/{layer_id}/asked", operation_id="get_drone_photo_questions_others_asked")
async def get_drone_photo_questions_others_asked(
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """Questions other people in farming asked Sage, in general words, a few offered today (asked_questions)."""
    reader = session.get_user_id() or "anonymous"
    return {"questions": await asked_questions.offered("agriculture", reader, layer.layer_id)}


class PlotSource(BaseModel):
    source: str  # "found" for the plots Ingabe finds in the photo, or the layer id of the reader's plot map


@router.put("/layer/{layer_id}/plots/source", operation_id="choose_drone_plot_source")
async def choose_drone_plot_source(
    choice: PlotSource,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, str]:
    """Use the reader's own plot map for this photo's plots, or go back to the plots Ingabe found."""
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    fields = await photo_plots.outlined_fields(s3, get_bucket_name(), layer.layer_id, metadata)
    if choice.source == photo_plots.FOUND and fields is None:
        sql, args = "UPDATE map_layers SET metadata = metadata - $2::text WHERE layer_id = $1", (layer.layer_id, photo_plots.PLOT_MAP_KEY)
    else:
        # With outlined fields on the photo the choice is kept, "found" too: it says the reader chose.
        maps = [fields] if fields else []
        if choice.source not in (photo_plots.FOUND, photo_plots.FIELDS):
            if not layer.bounds:
                raise HTTPException(409, "The photo's outline is not known yet")
            async with async_read_conn("drone_plot_maps", user_id=session.get_user_id()) as conn:
                maps += await photo_plots.plot_maps(conn, layer.layer_id, list(layer.bounds))
        if choice.source != photo_plots.FOUND and choice.source not in {m.layer_id for m, _ in maps}:
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


def _cog_key(metadata: dict[str, Any]) -> str:
    key = metadata.get("cog_key")
    if not key:
        raise HTTPException(409, "The photo is still being processed")
    return str(key)


def _place(layer: MapLayer) -> str:
    """The field's name as a person says it: the layer's name without "Orthophoto" and file-like endings."""
    name = re.sub(r"(?i)[_ -]*ortho(photo|mosaic)?[_ -]*\d*", " ", layer.name or "").replace("_", " ").strip()
    return re.sub(r"\s+", " ", name) or "the field"


async def _field_look(layer: MapLayer, metadata: dict[str, Any]) -> dict[str, Any]:
    redis, s3, key = get_async_redis(), await get_async_s3_client(), _cog_key(metadata)
    result = await close_look.load_raster(s3, get_bucket_name(), key)
    working = result is None and await close_look.running(redis, f"raster:{close_look.raster_key(key)}")
    if result:
        result = {**result, "squares": [{**sq, "picture": f"/api/layer/{layer.layer_id}/close-look/squares/{sq['n']}.jpg"}
                                        for sq in result["squares"]]}
    status = "ready" if result else "working" if working else "off" if not close_look.enabled() else "none"
    return {"status": status, "result": result}


@router.get("/layer/{layer_id}/close-look", operation_id="get_field_close_look")
async def get_field_close_look(layer: MapLayer = Depends(get_layer),
                               session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The close look kept for a drone photo: the whole field, and squares of it pinned to their places."""
    return await _field_look(layer, _photo_ready(layer))


@router.post("/layer/{layer_id}/close-look", operation_id="start_field_close_look")
async def start_field_close_look(layer: MapLayer = Depends(edit_layer),
                                 session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Start the close look at a drone photo (once: a kept look is not asked again). Returns its state."""
    metadata = _photo_ready(layer)
    s3 = await get_async_s3_client()
    await close_look.ensure_raster(get_async_redis(), s3, get_bucket_name(), _cog_key(metadata),
                                   await _cog_url(s3, metadata, _LONG_URL_SECONDS), _place(layer))
    return await _field_look(layer, metadata)


@router.get("/layer/{layer_id}/close-look/squares/{number}.jpg", operation_id="field_close_look_square")
async def field_close_look_square(number: int, layer: MapLayer = Depends(get_layer),
                                  session: UserContext = Depends(verify_session_required)) -> Response:
    """The picture of one square the close look was shown."""
    s3, key = await get_async_s3_client(), _cog_key(_photo_ready(layer))
    try:
        obj = await s3.get_object(Bucket=get_bucket_name(), Key=f"{close_look.raster_key(key)}/square{number}.jpg")
    except s3.exceptions.NoSuchKey:
        raise HTTPException(404, f"No square {number} looked at on this photo") from None
    async with obj["Body"] as body:
        picture = await body.read()
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


@router.delete("/layer/{layer_id}/records/{document_id}", operation_id="remove_farm_record")
async def remove_farm_record(
    document_id: str,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """Take a soil lab report or harvest records out of the photo's project records, for good: its rows leave the
    cards, Sage's findings and the fields they were linked to. Only an owner or editor of that project may."""
    project_id = await _records_project(layer.layer_id, session.get_user_id())
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    removed = await farm_records.remove_document(s3, bucket, project_id, document_id)
    if removed is None:
        raise HTTPException(404, "No such document in this farm's records")
    gone, file_deleted = removed
    try:
        await record_links.forget(s3, bucket, project_id, gone)
    except Exception:  # the rows are gone, so a link left behind points at nothing and is read by nothing
        logger.exception("farm document %s: its links could not be dropped", document_id)
    # file_deleted False: the document is out of every card, but its file is still in storage (logged for clean-up)
    return {"removed": document_id, "soil_samples": len(gone.soil_samples), "harvests": len(gone.harvests),
            "file_deleted": file_deleted}


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
                                              field_checks.checks_set_id(photo_plots.photo_key(metadata), plots,
                                                                         layer.layer_id),
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


class BandsIn(BaseModel):
    order: list[str] = Field(min_length=1, max_length=64)  # one of band_names.ROLES for each band, in file order
    thermal_unit: Optional[str] = None  # "celsius" or "kelvin", for a thermal band


NAMING_HOURS = 2  # a rewrite that started longer ago than this has stopped: the reader may start another
MAX_NAMED_BYTES = 8 * 1024 ** 3  # a larger photo is not rewritten by this route


async def _save_layer_details(layer_id: str, put: dict[str, Any], drop: tuple[str, ...] = ()) -> None:
    """Change some of a layer's stored details. They are how every worker knows a rewrite is running or failed."""
    async with get_async_db_connection() as conn:
        row = await conn.fetchrow("SELECT metadata FROM map_layers WHERE layer_id = $1", layer_id)
        metadata: dict[str, Any] = {}
        if row and row["metadata"]:
            metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"])
        metadata.update(put)
        for key in drop:
            metadata.pop(key, None)
        await conn.execute("UPDATE map_layers SET metadata = $1, last_edited = CURRENT_TIMESTAMP WHERE layer_id = $2",
                           json.dumps(metadata), layer_id)


def _naming_now(metadata: dict[str, Any]) -> bool:
    started = (metadata.get("bands_naming") or {}).get("at")
    try:
        return bool(started) and datetime.now(timezone.utc) - datetime.fromisoformat(started) < timedelta(hours=NAMING_HOURS)
    except (TypeError, ValueError):
        return False


def _unnamed_key(metadata: dict[str, Any]) -> str:
    """The photo as it was uploaded and processed, before any reader named its bands. It is never written over."""
    return metadata.get("cog_key_before_names") or metadata["cog_key"]


async def _bands_state(key: str, order: Optional[list[str]] = None) -> dict[str, Any]:
    """What the stored photo at `key` says of its bands; with `order`, also whether a band the reader calls thermal
    could hold temperatures."""
    url = await _cog_url(await get_async_s3_client(), {"cog_key": key})

    def read() -> dict[str, Any]:
        with rasterio.Env(**GDAL_HTTP_TIMEOUTS), rasterio.open(url) as ds:
            state = band_names.state(ds)
            if order is not None and len(order) == state["band_count"]:
                state["thermal_believable"] = band_names.thermal_is_believable(ds, order)
            return state

    try:
        return await asyncio.wait_for(asyncio.to_thread(read), timeout=60)
    except asyncio.TimeoutError:
        raise HTTPException(503, "The photo could not be opened in time. Try again.") from None


@router.get("/layer/{layer_id}/bands", operation_id="get_drone_photo_bands")
async def get_drone_photo_bands(
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """What the photo's file says of its bands, whether the reader has to say which band is which, and what a
    reader said before (which they may change: the photo as uploaded is kept)."""
    metadata = _photo_ready(layer)
    state = await _bands_state(metadata["cog_key"])
    said = metadata.get("bands_said") or None
    state["said"] = {"order": said.get("order"), "thermal_unit": said.get("thermal_unit"), "at": said.get("at")} if said else None
    state["can_change"] = bool(metadata.get("cog_key_before_names"))
    if state["can_change"]:
        state["presets"] = band_names.PRESETS.get(state["band_count"], [])
    state["working"] = _naming_now(metadata)
    state["failed"] = bool(metadata.get("bands_naming_failed")) and not state["working"]
    return state


@router.put("/layer/{layer_id}/bands", status_code=202, operation_id="say_drone_photo_bands")
async def say_drone_photo_bands(
    body: BandsIn,
    layer: MapLayer = Depends(edit_layer),
    session: UserContext = Depends(verify_session_required),
) -> dict[str, Any]:
    """The reader says which band is which, for a photo whose file does not. A copy of the photo is written with
    those names beside the request (minutes for a large photo) and this layer is pointed at it. The photo as it
    was uploaded is kept, so the reader can say otherwise later, and no other layer of that photo changes."""
    metadata = _photo_ready(layer)
    if _naming_now(metadata):
        raise HTTPException(409, "The band names are already being written for this photo")
    source_key = _unnamed_key(metadata)
    state = await _bands_state(source_key, body.order)
    if not state["needs_names"]:
        raise HTTPException(409, "This photo's file says which band is which itself")
    try:
        band_names.check(body.order, state["band_count"], body.thermal_unit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if not state.get("thermal_believable", True):
        raise HTTPException(422, "The values of that band cannot be temperatures. A height map is one band too: "
                                 "it is not a thermal photo.")
    bucket, layer_id, user_id = get_bucket_name(), layer.layer_id, session.get_user_id()
    size = int((await (await get_async_s3_client()).head_object(Bucket=bucket, Key=source_key)).get("ContentLength") or 0)
    if size > MAX_NAMED_BYTES or shutil.disk_usage(tempfile.gettempdir()).free < 3 * size:
        raise HTTPException(413, "This photo is too large to be written again with band names here")
    now = datetime.now(timezone.utc).isoformat()
    await _save_layer_details(layer_id, {"bands_naming": {"at": now, "by": user_id}}, drop=("bands_naming_failed",))
    stamp = hashlib.sha256(f"{layer_id}|{'|'.join(body.order)}|{body.thermal_unit}|{now}".encode()).hexdigest()[:16]
    named_key = f"{source_key}.named-{stamp}.tif"

    async def work(progress: background_jobs.Progress) -> None:
        def rewrite() -> None:
            client = get_s3_client()
            with tempfile.TemporaryDirectory() as folder:
                source, target = os.path.join(folder, "photo.tif"), os.path.join(folder, "named.tif")
                client.download_file(bucket, source_key, source)
                band_names.rewrite(source, target, body.order, body.thermal_unit, folder)
                client.upload_file(target, bucket, named_key, ExtraArgs={"ContentType": "image/tiff"})

        try:
            await asyncio.to_thread(rewrite)
        except Exception:
            await _save_layer_details(layer_id, {"bands_naming_failed": datetime.now(timezone.utc).isoformat()},
                                      drop=("bands_naming",))
            raise
        # only this layer now reads the named copy; everything kept by photo starts afresh under its new key
        await _save_layer_details(
            layer_id,
            {"cog_key": named_key, "cog_key_before_names": source_key,
             "bands_said": {"order": body.order, "thermal_unit": body.thermal_unit, "by": user_id, "at": now}},
            drop=("bands_naming",))
        await tile_cache.invalidate_layer(layer_id)

    background_jobs.start(f"band-names:{layer_id}", work)
    return {"working": True}


@router.get("/layer/{layer_id}/zones.geojson", operation_id="download_drone_field_zones")
async def download_drone_field_zones(
    layer: MapLayer = Depends(get_layer),
    session: UserContext = Depends(verify_session_required),
) -> Response:
    """The zones inside the photo's fields, for the agronomist: one feature per zone, with an empty rate."""
    _, here, _ = await _photo(layer, session, None)
    if here.zones is None or not here.zones["geojson"]["features"]:
        raise HTTPException(409, "This photo has no field zones yet")
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", layer.name or layer.layer_id).strip("_")[:60] + "_zones"
    features = [{**f, "properties": {k: v for k, v in f["properties"].items() if k != "group"}}  # "group" is the
                for f in here.zones["geojson"]["features"]]  # map's colour key, not a fact about the zone
    return Response(content=json.dumps({"type": "FeatureCollection", "features": features}),
                    media_type="application/geo+json",
                    headers={"Content-Disposition": f'attachment; filename="{stem}.geojson"',
                             "Cache-Control": "private, no-store"})


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
