"""Field outlines from taps on a drone photo. An adapter only: the grid, the tap model, the rules and the kept
outlines live in src/services/field_taps.py. Switch FIELD_TAPS (off: every route here answers 404)."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.database.models import LAYER_TYPE_RASTER, MapLayer
from src.dependencies.dag import edit_layer, get_layer
from src.dependencies.session import UserContext, verify_session_required
from src.routes.switched_routes import switched_route
from src.services import (
    count_check,
    farm_records,
    field_facts,
    field_flights,
    field_health,
    field_taps,
    industry,
    my_crop,
    photo_context,
    photo_plots,
    plant_finder,
    record_links,
)
from src.structures import async_read_conn, get_async_db_connection
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

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


class PlaceIn(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class MyCropIn(BaseModel):
    crop: list[PlaceIn] = Field(min_length=my_crop.MIN_TAPS, max_length=my_crop.MAX_TAPS,
                                description="taps on plants of the reader's crop")
    other: list[PlaceIn] = Field(min_length=my_crop.MIN_TAPS, max_length=my_crop.MAX_TAPS,
                                 description="taps on green things that are not the reader's crop")


Square = tuple[Annotated[int, Field(ge=0, le=100_000)], Annotated[int, Field(ge=0, le=100_000)]]


class CheckIn(BaseModel):
    square: Square
    shown: str = Field(min_length=16, max_length=16, description="the plants_mark of the part the reader was shown")
    wrong: list[Annotated[int, Field(ge=0, le=100_000)]] = Field(
        max_length=5000, description="the app's rings that are not plants, by their place in the square's list")
    missed: list[PlaceIn] = Field(max_length=count_check.MAX_MISSED, description="plants that got no ring")


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
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    # a link that outlasts the background job measuring green cover, as the other background photo readers use
    cog_url = await photo_context.cog_url(s3, bucket, layer.metadata_dict or {}, photo_context.LONG_URL_SECONDS)
    state = await field_taps.state(s3, bucket, key, layer.layer_id, cog_url)
    thermal_photos: list[dict[str, Any]] = []
    try:
        async with async_read_conn("field_taps.thermal", user_id=session.get_user_id()) as conn:
            thermal_photos = await photo_context.one_band_photos_here(conn, layer.layer_id, layer.bounds, key)
    except Exception:  # the fields are still shown, without a temperature from another photo
        logger.exception("field taps: the thermal photos of this place could not be listed")
    added, other_flights = None, []
    try:
        async with async_read_conn("field_taps.flights", user_id=session.get_user_id()) as conn:
            added, other_flights = await photo_context.flights_here(conn, layer.layer_id, layer.bounds, key)
    except Exception:  # the fields are still shown, each on this photo alone
        logger.exception("field taps: the other flights of this place could not be listed")
    this_photo = {"name": layer.name, "metadata": layer.metadata_dict or {}, "uploaded": added}
    apart, unsure = field_flights.other_flights(this_photo, other_flights)
    state["kept"] = await _with_facts(state["kept"], s3, bucket, key, layer.layer_id, session.get_user_id(), cog_url,
                                      thermal_photos, apart, this_photo, layer.bounds)
    # photos of this place added the same day with no flight day given: not known to be other flights
    state["photos_not_told_apart"] = unsure
    given = field_flights.flown_on(layer.metadata_dict or {})
    state["flown_on"] = given.isoformat() if given else None
    # what the photo's own file says of its day, offered to a reader who has not given one (never taken for them)
    state["file_day"] = None
    if given is None:
        added_as, name = cast(Optional[str], layer.s3_key), cast(Optional[str], layer.name)
        names = ((layer.metadata_dict or {}).get("original_filename"), name)
        try:
            copies = [cog_url] if cog_url else []
            if added_as and added_as != key:  # the file as it was added comes before the app's copy of it
                copies.insert(0, await s3.generate_presigned_url(
                    "get_object", Params={"Bucket": bucket, "Key": added_as}, ExpiresIn=900))
            read, in_file = await field_flights.day_in_file(s3, bucket, key, copies)
        except Exception as why:  # noqa: BLE001 - the fields are still shown, and the day is asked as before
            # no traceback: a storage error's words can hold a signed link
            logger.warning("field taps: the photo's file could not be asked for its day (%s)", type(why).__name__)
            read, in_file = False, None
        state["file_day"] = field_flights.file_day_offered(
            in_file, names, datetime.now(timezone.utc).date() + timedelta(days=1), file_read=read)
    state["flights_note"] = field_flights.NOTE
    state["flights_measuring"] = any(entry["said"] == field_flights.NOT_YET
                                     for row in state["kept"] for entry in (row.get("facts") or {}).get("flights", ()))
    return state


class FlownIn(BaseModel):
    day: Optional[date] = Field(default=None, description="the day of the flight; null takes it off")


@router.put("/layer/{layer_id}/field-taps/flown-on", operation_id="say_photo_flight_day")
async def say_photo_flight_day(body: FlownIn, layer: MapLayer = Depends(edit_layer),
                               session: UserContext = Depends(verify_session_required)) -> dict[str, Optional[str]]:
    """The day this photo was flown, as the reader gives it: what a field's flights are put in order by. Without it
    a photo is said by the day it was added."""
    await _photo_key(layer)
    if body.day is None:
        sql = "UPDATE map_layers SET metadata = (metadata - $2::text) - $3::text WHERE layer_id = $1"
        args: tuple[Any, ...] = (layer.layer_id, field_flights.FLOWN_ON, field_flights.FLOWN_ON_BY)
    else:
        try:  # a day ahead of the server's clock by a time zone is still today somewhere
            field_flights.check_day(body.day, datetime.now(timezone.utc).date() + timedelta(days=1))
        except ValueError as why:
            raise HTTPException(422, str(why)) from why
        # who gave the day and when is kept with it: it is a person's word, not a fact read from the file
        sql = ("UPDATE map_layers SET metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object("
               "$2::text, $3::text, $4::text, jsonb_build_object('by', $5::text, 'at', $6::text)) WHERE layer_id = $1")
        args = (layer.layer_id, field_flights.FLOWN_ON, body.day.isoformat(), field_flights.FLOWN_ON_BY,
                session.get_user_id(), datetime.now(timezone.utc).isoformat(timespec="seconds"))
    async with get_async_db_connection() as conn:
        await conn.execute(sql, *args)
    return {"flown_on": body.day.isoformat() if body.day else None}


async def _project(layer_id: str, user_id: str, to_edit: bool = False) -> Optional[str]:
    """The project whose records this photo reads, as the user may see it. `to_edit`: refused unless the user owns
    or edits that very project (edit_layer passes when they edit any project the layer is in)."""
    async with async_read_conn("field_taps.project", user_id=user_id) as conn:
        project_id = await photo_context.project_of(conn, layer_id)
        if project_id and to_edit:
            row = await conn.fetchrow("SELECT owner_uuid, editor_uuids FROM user_mundiai_projects WHERE id = $1", project_id)
            editors = [str(u) for u in (row["editor_uuids"] or [])] if row else []
            if row is None or user_id not in [str(row["owner_uuid"]), *editors]:
                raise HTTPException(403, "You do not have edit access to this farm's records")
    return project_id


async def _with_facts(rows: list[dict[str, Any]], s3: Any, bucket: str, key: str, layer_id: str,
                      user_id: str, cog_url: Optional[str] = None,
                      thermal_photos: Optional[list[dict[str, Any]]] = None,
                      other_flights: Optional[list[dict[str, Any]]] = None,
                      this_photo: Optional[dict[str, Any]] = None, bounds: Any = None) -> list[dict[str, Any]]:
    """Each kept field with what is known of it in the words every surface uses (field_facts): its size, its
    plant count and a person's check of it once Sage counted it, and the rows of the farm's documents linked to it
    by place, and what the photo's kept satellite view says of it (`bounds`: the photo's place). Reads only; a field
    whose facts cannot be read is still shown."""
    if not rows:
        return rows
    known: dict[str, field_facts.FieldFacts] = {}
    linked: list[record_links.Linked] = []
    try:  # the documents' rows on their own: if they cannot be read, the fields keep their size, count and check
        linked = await record_links.of_project(s3, bucket, await _project(layer_id, user_id),
                                               record_links.kept_features(rows))
    except Exception:
        logger.exception("field taps: the documents' rows could not be placed on the kept fields")
    health: dict[str, field_health.FieldHealth] = {}
    if cog_url:
        try:  # {} at once for a photo with no near-infrared band, after its first look
            health = await field_health.of_features(s3, bucket, key, cog_url, rows)
        except Exception:
            logger.exception("field taps: the health of the kept fields could not be measured")
    if thermal_photos:
        health = await field_health.with_bands(s3, bucket, health, rows, thermal_photos,
                                               lambda companion: photo_context.long_cog_url(s3, bucket, companion))
        health = await field_health.with_spectral(s3, bucket, health, rows, thermal_photos,
                                                  lambda companion: photo_context.long_cog_url(s3, bucket, companion), key)
        health = await field_health.with_thermal(s3, bucket, health, rows, thermal_photos,
                                                 lambda companion: photo_context.long_cog_url(s3, bucket, companion))
    flights: dict[str, list[dict[str, Any]]] = {}
    if other_flights:
        try:  # the same outlines on the other flights of this place
            flights = await field_flights.of_features(
                s3, bucket, rows, other_flights, lambda companion: photo_context.long_cog_url(s3, bucket, companion))
        except Exception:
            logger.exception("field taps: the other flights of the kept fields could not be read")
    try:
        known = await field_facts.of_kept(s3, bucket, key, layer_id, record_links.by_field(linked), health, flights,
                                          this_photo, await photo_context.satellite_of_fields(bounds, rows))
    except Exception:
        logger.exception("field taps: the facts of the kept fields could not be read")
    return [{**row, "facts": {"name": known[row["id"]].name, "area_said": known[row["id"]].area_said, "plants_said": known[row["id"]].plants_said,
                              "checked_said": known[row["id"]].checked_said,
                              "health_said": known[row["id"]].health_said,
                              "health_note": (field_health.health_note(known[row["id"]].health_from)
                                              if known[row["id"]].health_said else None),
                              "temperature_said": known[row["id"]].temperature_said,
                              "temperature_note": (field_health.temperature_note(known[row["id"]].temperature_from)
                                                   if known[row["id"]].temperature_said else None),
                              "satellite_note": known[row["id"]].satellite_note,
                              "flights": list(known[row["id"]].flights),
                              "records_said": list(known[row["id"]].records_said)}} if row["id"] in known else row
            for row in rows]


class LinkIn(BaseModel):
    row: str = Field(min_length=3, max_length=80)
    field_id: str = Field(min_length=1, max_length=64)
    linked: bool = True  # False: this row is not this field (also sets aside a position the document prints in it)


async def _records_and_fields(layer: MapLayer, user_id: str,
                              to_edit: bool = False) -> tuple[Any, str, str, list[dict[str, Any]]]:
    key = await _photo_key(layer)
    project_id = await _project(layer.layer_id, user_id, to_edit)
    if not project_id:
        raise HTTPException(404, "This photo is not in a project")
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    return s3, bucket, project_id, await field_taps.kept(s3, bucket, key, layer.layer_id)


@router.get("/layer/{layer_id}/field-taps/records", operation_id="get_field_records")
async def get_field_records(layer: MapLayer = Depends(get_layer),
                            session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Every row of the farm's documents (lab samples, harvests) with the field it is linked to, if any. A row is
    linked by the position its document prints or by a person, never by a matching name or number."""
    s3, bucket, project_id, kept = await _records_and_fields(layer, session.get_user_id())
    linked = await record_links.of_project(s3, bucket, project_id, record_links.kept_features(kept))
    ids = {row["number"]: row["id"] for row in kept}
    return {"rows": [{"key": item.row.key, "kind": item.row.kind, "label": item.row.label, "value": item.row.value,
                      "document": item.row.document, "said": record_links.row_said(item),
                      "field": item.number, "field_id": ids.get(item.number) if item.number is not None else None,
                      "by": item.by, "suggest": item.suggest,
                      "suggest_id": ids.get(item.suggest) if item.suggest is not None else None,
                      "suggest_why": item.suggest_why, "note": item.note, "elsewhere": item.elsewhere}
                     for item in linked],
            "not_linked": record_links.not_linked(linked)}


@router.post("/layer/{layer_id}/field-taps/records/link", operation_id="link_field_record")
async def link_field_record(body: LinkIn, layer: MapLayer = Depends(edit_layer),
                            session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """A person says a document row is this field, or is not. The field's outline is kept, not its number, so the
    link holds when fields are numbered differently later."""
    s3, bucket, project_id, kept = await _records_and_fields(layer, session.get_user_id(), to_edit=True)
    known = {row.key for row in record_links.rows(await farm_records.load_records(s3, bucket, project_id))}
    if body.row not in known:
        raise HTTPException(404, "No such row in this farm's documents")
    field = next((row for row in kept if row["id"] == body.field_id), None)
    if field is None:
        raise HTTPException(404, "No such kept outline on this photo")
    await record_links.keep(s3, bucket, project_id, body.row, field["geometry"], body.linked, session.get_user_id())
    return {"row": body.row, "field": field["number"] if body.linked else None}


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
        s3, bucket = await get_async_s3_client(), get_bucket_name()
        row = await field_taps.keep(s3, bucket, key, layer.layer_id, _taps(body), body.pick, session.get_user_id(),
                                    body.moved, await photo_context.cog_url(s3, bucket, layer.metadata_dict or {}))
        return field_taps.shown(row)
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
        row = await field_taps.turn_down(await get_async_s3_client(), get_bucket_name(), key, layer.layer_id,
                                         _taps(body), body.pick, body.reason, session.get_user_id())
        return {k: v for k, v in row.items() if k != "turned_down_by"}
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None


@router.delete("/layer/{layer_id}/field-taps/kept/{outline_id}", operation_id="remove_field_outline")
async def remove_field_outline(outline_id: str, layer: MapLayer = Depends(edit_layer),
                               session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    key = await _photo_key(layer)
    if not await field_taps.remove(await get_async_s3_client(), get_bucket_name(), key, layer.layer_id, outline_id):
        raise HTTPException(404, "No such kept outline on this photo")
    return {"removed": outline_id}


class NameIn(BaseModel):
    name: str = Field(max_length=200)  # empty: take the name off


@router.put("/layer/{layer_id}/field-taps/kept/{outline_id}/name", operation_id="name_field_outline")
async def name_field_outline(outline_id: str, body: NameIn, layer: MapLayer = Depends(edit_layer),
                             session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Give a kept field the farm's own name for it (empty: take it off). It is said with the field's number on the
    panel, the cards and to Sage, and a document that uses the same name is offered for the field, never linked."""
    key = await _photo_key(layer)
    try:
        row = await field_taps.name_field(await get_async_s3_client(), get_bucket_name(), key, layer.layer_id,
                                          outline_id, body.name)
    except field_taps.TapError as e:
        raise HTTPException(422, str(e)) from None
    if row is None:
        raise HTTPException(404, "No such kept outline on this photo")
    return field_taps.shown(row)


@router.post("/layer/{layer_id}/field-taps/kept/{outline_id}/my-crop", operation_id="count_my_crop_in_field")
async def count_my_crop_in_field(outline_id: str, body: MyCropIn, layer: MapLayer = Depends(edit_layer),
                                 session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The plants of the reader's crop in one kept field, sorted by their taps; {"working": true, "done", "total"}
    while the field's squares are being read in the background (ask again with the same taps). For people who may
    edit the layer: the first ask of a field starts minutes of work and writes to storage."""
    key = await _photo_key(layer)
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    row = next((r for r in await field_taps.kept(s3, bucket, key, layer.layer_id) if r["id"] == outline_id), None)
    if row is None:
        raise HTTPException(404, "No such kept outline on this photo")
    feature = {"type": "Feature", "geometry": row["geometry"], "properties": {"area_ha": row.get("area_ha", 0.0)}}
    url = await photo_context.cog_url(s3, bucket, layer.metadata_dict or {}, photo_context.LONG_URL_SECONDS)
    try:
        return await my_crop.count(url, feature, get_s3_client(), bucket, key,
                                   [(p.lon, p.lat) for p in body.crop], [(p.lon, p.lat) for p in body.other])
    except (my_crop.TapProblem, plant_finder.CannotCount) as e:
        raise HTTPException(422, str(e)) from None
    except my_crop.Busy as e:
        raise HTTPException(503, str(e)) from None


async def _kept_field(layer: MapLayer, outline_id: str) -> tuple[str, Any, str, dict[str, Any]]:
    key = await _photo_key(layer)
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    row = next((r for r in await field_taps.kept(s3, bucket, key, layer.layer_id) if r["id"] == outline_id), None)
    if row is None:
        raise HTTPException(404, "No such kept outline on this photo")
    return key, s3, bucket, row


@router.get("/layer/{layer_id}/field-taps/kept/{outline_id}/count-check", operation_id="get_field_count_checks")
async def get_field_count_checks(outline_id: str, layer: MapLayer = Depends(get_layer),
                                 session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """What the reader's own checks of the plant count say for this field: {"checked": null} when there are none."""
    key, s3, bucket, row = await _kept_field(layer, outline_id)
    return {"checked": await count_check.summary(s3, bucket, key, layer.layer_id, row), "measured": count_check.MEASURED}


@router.post("/layer/{layer_id}/field-taps/kept/{outline_id}/count-check/square", operation_id="next_square_to_check")
async def next_square_to_check(outline_id: str, layer: MapLayer = Depends(edit_layer),
                               session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """The next part of a kept field to check, chosen by the app, with the plants the count holds there. For
    people who may edit the layer: it runs the plant finder."""
    key, s3, bucket, row = await _kept_field(layer, outline_id)
    url = await photo_context.cog_url(s3, bucket, layer.metadata_dict or {}, photo_context.LONG_URL_SECONDS)
    try:
        return await count_check.square(url, s3, get_s3_client(), bucket, key, layer.layer_id, row)
    except (count_check.CheckProblem, plant_finder.CannotCount) as e:
        raise HTTPException(422, str(e)) from None
    except count_check.Busy as e:
        raise HTTPException(503, str(e)) from None


@router.post("/layer/{layer_id}/field-taps/kept/{outline_id}/count-check", operation_id="keep_field_count_check")
async def keep_field_count_check(outline_id: str, body: CheckIn, layer: MapLayer = Depends(edit_layer),
                                 session: UserContext = Depends(verify_session_required)) -> dict[str, Any]:
    """Keep the reader's check of one square: the rings that are not plants and the plants that got no ring."""
    key, s3, bucket, row = await _kept_field(layer, outline_id)
    url = await photo_context.cog_url(s3, bucket, layer.metadata_dict or {}, photo_context.LONG_URL_SECONDS)
    try:
        return await count_check.keep(url, s3, get_s3_client(), bucket, key, layer.layer_id, row, body.square,
                                      body.shown, body.wrong, [(p.lon, p.lat) for p in body.missed],
                                      session.get_user_id())
    except (count_check.CheckProblem, plant_finder.CannotCount) as e:
        raise HTTPException(422, str(e)) from None
    except count_check.Busy as e:
        raise HTTPException(503, str(e)) from None
