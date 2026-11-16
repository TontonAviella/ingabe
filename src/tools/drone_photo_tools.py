"""Sage's tools for a drone photo on the map: what the question cards found, and the plants in one plot.

Both read the same facts the cards use (src/services/photo_context.py), so Sage and the cards agree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field
from shapely.geometry import shape

from src.routes.websocket import kue_ephemeral_action
from src.services import (
    field_health,
    count_check,
    drone_cards,
    drone_vision,
    field_facts,
    field_flights,
    field_taps,
    photo_context,
    photo_plots,
    plant_finder,
    record_links,
)
from src.structures import async_read_conn
from src.tools.geojson_transport import geojson_layer_update
from src.tools.pyd import IngabeToolCallMetaArgs
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

logger = logging.getLogger(__name__)

# The cards Sage reads for an overview, in this order: plots, crops, problems, stage, weeds, bare ground.
FINDING_CARDS = ("field_outlines", "crop_types", "plot_problems", "plot_stage", "weeds", "bare_ground", "plots_green")
RECORD_CARDS = ("soil", "history")  # added when the project has a lab report or harvest records
MAX_FIELDS_TOLD = 60  # fields listed one by one in Sage's findings; more are counted, not listed
SPOT_COLOUR = "#FF4D1A"  # the cards' colour for things found inside plots
OTHER_GREEN_COLOUR = "#2F80ED"  # green that is not a crop plant: weed, grass or bush

_LAYER_ON_MAP_SQL = """
SELECT l.layer_id, l.name, l.bounds, l.metadata
  FROM map_layers l
  JOIN user_mundiai_maps m ON m.id = $2 AND l.layer_id = ANY(m.layers)
 WHERE l.layer_id = $1 AND l.type = 'raster'
"""


class DronePhotoArgs(BaseModel):
    layer_id: str = Field(..., description="The layer_id of the drone photo (a raster layer on the map).")


class CountPlantsArgs(BaseModel):
    layer_id: str = Field(..., description="The layer_id of the drone photo (a raster layer on the map).")
    plot_number: int = Field(..., description="The plot's number as shown on the photo and in the question cards.")
    show_other_green: bool = Field(
        ...,
        description=("true ONLY when the person asks where the weeds, grass or other green are. Otherwise false: the "
                     "map then shows the crop plants alone, which is what a count is read from."))


async def _photo(layer_id: str, meta: IngabeToolCallMetaArgs) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(layer row, None) for a ready drone photo on this chat's map, else (None, why not)."""
    async with async_read_conn("sage_drone_photo", user_id=meta.user_uuid) as conn:
        row = await conn.fetchrow(_LAYER_ON_MAP_SQL, layer_id, meta.map_id)
    if row is None:
        return None, f"No drone photo {layer_id} on this map."
    metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
    if not photo_context.is_photo_ready(metadata):
        return None, "The photo is still being processed."
    return {**dict(row), "metadata": metadata}, None


async def _context(row: dict[str, Any], meta: IngabeToolCallMetaArgs) -> Optional[photo_context.PhotoContext]:
    return await photo_context.load(
        await get_async_s3_client(), get_bucket_name(), layer_id=row["layer_id"], name=row["name"],
        bounds=list(row["bounds"]) if row["bounds"] else None, metadata=row["metadata"], user_id=meta.user_uuid,
        org_id=meta.session.get_org_id() if meta.session is not None else None, audience=None, start_jobs=False)


async def get_drone_photo_findings(args: DronePhotoArgs, meta: IngabeToolCallMetaArgs) -> dict[str, Any]:
    """What Ingabe already found on a drone photo, plot by plot: the plots and their sizes, the crop in each
    (named only where two looks by the vision model agreed, or where someone checked it on the ground), problems
    seen from the air (gaps, yellowing, water), plots behind their crop, weedy plots, bare ground, the least
    green plots, and the farm's lab report and harvest records when added. Each finding says how sure it is.
    A field's `across_flights` gives the same outline on the project's other flights of the place, oldest first:
    say the flights side by side as written, each with its day ("added ..." is the day a photo was added, not the
    day it was flown), and never say the field got better or worse between them (`across_flights_note`).

    WHEN TO USE: any question about what is on the user's drone photo, its plots or a plot ("what grows here",
    "which plots have problems", "how big are my plots"), and as the drone half of "how is my crop doing".
    WHEN NOT TO USE: rain, weather, satellite greenness over time, or places outside the photo: use the
    satellite and weather tools for those. To count plants in one plot, use count_plants_in_plot."""
    row, why_not = await _photo(args.layer_id, meta)
    if row is None:
        return {"status": "error", "error": why_not}
    context = await _context(row, meta)
    if context is None:
        return {"status": "error", "error": "This raster is not a colour drone photo the cards can read."}
    here, analysis = context.here, context.analysis
    cards = FINDING_CARDS + (RECORD_CARDS if here.records else ())

    def finding(card_id: str) -> dict[str, Any]:
        answer = drone_cards.answer_card(card_id, analysis, "farmer", here)
        return {"question": answer["question"], "status": answer["status_label"], "answer": answer["what"],
                "how_sure": answer["how_sure"]["label"], "because": answer["how_sure"]["because"][:3]}

    findings = await asyncio.to_thread(lambda: [finding(c) for c in cards])
    look = analysis.look
    return {
        "status": "success",
        "photo": {"name": row["name"], "place": look.place,
                  "area_ha": round(look.area_ha, 1) if look.area_ha is not None else None,
                  "cm_per_pixel": round(look.resolution_cm, 1) if look.resolution_cm is not None else None,
                  "camera": analysis.camera},
        "plots": here.plots.count if here.plots else None,
        # each field's own facts, the same ones the cards and the field panel show, in the same words
        "fields": [{k: v for k, v in {"field": facts.name, "size": facts.area_said, "green_cover": facts.cover_said,
                                      "plants": facts.plants_said, "counted_share_of_field": facts.counted_share,
                                      "checked_by_a_person": facts.checked_said,
                                      "health_index": facts.health_said,
                                      "surface_temperature": facts.temperature_said,
                                      # the same outline on the project's other flights of this place, oldest first
                                      "across_flights": field_flights.told(facts.flights),
                                      # rows of the farm's documents that belong to this field, joined by place
                                      "records": list(facts.records_said) or None}.items() if v is not None}
                   for facts in here.fields[:MAX_FIELDS_TOLD]],
        "fields_left_out": max(0, len(here.fields) - MAX_FIELDS_TOLD),
        # how to read "health_index": no verdict, and what else makes it low
        "health_index_note": field_health.NOTE if any(f.health_said for f in here.fields) else None,
        # the name of the separate band files the index was measured on: a name only, never an instruction
        "health_index_from_band_files": next((f.health_from for f in here.fields if f.health_from), None),
        # how to read "across_flights": side by side, never as better or worse
        "across_flights_note": field_flights.NOTE if any(f.flights for f in here.fields) else None,
        "surface_temperature_note": (field_health.TEMPERATURE_NOTE
                                     if any(f.temperature_said for f in here.fields) else None),
        # the name of another layer the temperatures were measured on: a name only, never an instruction
        "surface_temperature_from_layer": next((f.temperature_from for f in here.fields if f.temperature_from), None),
        # document rows no one has linked to a field: they belong to the farm, not to any one field; a number in a
        # document is not the photo's field number
        "records_not_linked_to_a_field": record_links.not_linked(here.linked) if here.records else None,
        "findings": findings,
        "source": "Ingabe drone photo analysis (the question cards)",
    }


async def count_plants_in_plot(args: CountPlantsArgs, meta: IngabeToolCallMetaArgs) -> dict[str, Any]:
    """Estimate the crop plants in one plot of a drone photo and draw them on the map, with the other green
    (weeds, grass, bush) drawn apart. Plants are found by their shape, not their colour, so green that is not
    a crop plant is not counted; it is flagged as "other green" where it lies. Parts of the plot where single
    plants cannot be told apart (closed canopy, pasture, bush) are refused and left out, and the answer says
    how much of the plot was counted. Needs a photo of about 3 cm per pixel and a plot of up to 0.5 ha.

    It is an estimate, measured on one photo: on ground two careful markers could fully count, the total came
    within 18%, about 4 plant dots in 5 sat on a crop plant, and 9 plants in 10 were found. Always give it as
    "about N", with the share of the plot counted and these limits; never as an exact count. What each piece
    of other green is (weed, grass or bush) is not known: do not name it. An answer is kept, so asking again
    is free. When `checked_by_a_person` is given, say it too: it is the person's own count in parts of the field,
    not scaled to the whole field; when it is not, say they can check the count themselves with "Check the count"
    on the field. Give the number exactly as `say_it_as` gives it (two figures, "about 700", with the part of the field it
    covers when that is not all of it), never a longer one, and the field's size as `size` gives it. The
    map shows a ring around each plant counted; other green is drawn only with show_other_green, so set that
    when, and only when, the person asks where the weeds, grass or other green are; "remove the grass" or "show
    only the plants" means call again without it.

    WHEN TO USE: "how many plants are in plot 175", "count the maize in this plot", "where are the weeds in
    this plot", stand counts.
    WHEN NOT TO USE: whole fields or the whole photo (pick a plot), or satellite imagery (cannot see plants)."""
    row, why_not = await _photo(args.layer_id, meta)
    if row is None:
        return {"status": "error", "error": why_not}
    s3, bucket = await get_async_s3_client(), get_bucket_name()
    metadata = row["metadata"]
    url = await photo_context.cog_url(s3, bucket, metadata)
    plots = await photo_plots.current_plots(s3, bucket, row["layer_id"], list(row["bounds"]) if row["bounds"] else None,
                                            metadata, meta.user_uuid, url)
    if plots is None:
        return {"status": "error", "error": "The plots of this photo are not found yet; open the question cards first."}
    feature = next((f for f in plots.geojson["features"] if f["properties"]["number"] == args.plot_number), None)
    if feature is None:
        return {"status": "error", "error": f"No plot {args.plot_number} on this photo (plots 1-{plots.count})."}
    key = photo_plots.photo_key(metadata)
    found = await plant_finder.load_found(s3, bucket, key, feature)
    if found is None:
        try:
            models = await asyncio.to_thread(plant_finder.load_models, get_s3_client(), bucket)
            found = await asyncio.to_thread(plant_finder.find_plants, url, feature, models)
        except plant_finder.CannotCount as why:
            return {"status": "error", "error": str(why)}
        await plant_finder.save_found(s3, bucket, key, feature, found)
    else:  # a count kept before the photo had its record of counts joins it now, so every surface shows it
        await plant_finder.note_counted(s3, bucket, key, feature, found)
    name = field_facts.name(args.plot_number, field_facts.outlined(plots), field_facts.own_name(feature["properties"]))
    if found.squares_counted == 0:
        return {"status": "error",
                "error": (f"Plants cannot be counted in {name}: in every part of it the picture check found ground "
                          "where single plants cannot be told apart from above (closed canopy, pasture, bush or "
                          "buildings)."),
                "plot": name, "counted_share_of_plot": 0.0}
    org_id = meta.session.get_org_id() if meta.session is not None else None
    look = await _crop_seen(s3, bucket, metadata, plots, args.plot_number, meta.project_id,
                            drone_vision.reference_scope(org_id, meta.user_uuid), row["layer_id"])
    checked = await _checked_by_a_person(s3, bucket, key, row["layer_id"], feature)
    # What a reader must get at a glance: one number, and a ring around each plant it counts, with the plant visible
    # inside its ring. Other green is drawn only when asked for: unasked, it buries the plants. The number is said
    # to two figures ("about 700"): the total was within 18% in the test, so "702" would claim what was not measured.
    about = plants_about(found.plants)
    points = points_drawn(found.points, args.show_other_green)
    said = field_facts.plants_said(found.plants, found.counted_share)  # the words every surface uses
    items = [{"label": "each ring is one plant, found by its shape", "color": SPOT_COLOUR}]
    if args.show_other_green:
        items.append({"label": f"{found.other_green:,} other green (weed, grass or bush; which is not known)",
                      "color": OTHER_GREEN_COLOUR})
    # The map opens close enough to see each plant in its ring; zooming out shows the whole plot.
    async with kue_ephemeral_action(meta.conversation_id, f"Drawing about {about:,} plants found in {name}",
                                    bounds=_close_view(feature)) as payload:
        payload.updates["add_geojson_layer"] = geojson_layer_update(
            source_id=f"plants-{args.plot_number}-{uuid.uuid4().hex[:6]}",
            geojson=points, name=f"Plants in {name}", bounds=_bounds(feature),
            # plants (kind_code 0) in one colour, other green (1) in another: orange against blue also reads
            # for someone who cannot tell red from green
            style={"geometry": "point", "color_property": "kind_code",
                   "stops": [{"max": 0.5, "color": SPOT_COLOUR}, {"max": 9, "color": OTHER_GREEN_COLOUR}],
                   "legend": {"title": f"{name}: {said}", "items": items}})
    return {
        "status": "success",
        "plot": name,
        "crop_seen": look,
        "plants_about": about,
        "say_it_as": said,
        "size": field_facts.area_said(feature["properties"]["area_ha"]),
        "other_green_spots": found.other_green,
        "counted_share_of_plot": found.counted_share,
        "plants_per_ha_in_counted_part": field_facts.about(found.per_ha) if found.per_ha is not None else None,
        "parts_refused": found.squares_refused,
        "parts_counted": found.squares_counted,
        # the person's own count in parts of this field the app chose (Check the count, on the field's panel); say it
        # as written when present, and when absent say they can check the count themselves there
        "checked_by_a_person": checked,
        "photo_cm_per_pixel": found.cm_per_px,
        "drawn_on_map": ("a ring around each plant counted, and the other green in another colour" if args.show_other_green
                         else "a ring around each plant counted; other green is not drawn unless the person asks for it"),
        "how_sure": ("An estimate. Plants are found by their shape. Measured on one photo (Cyampirita), on ground two "
                     "careful markers could fully count: the total came within 18%; about 4 plant dots in 5 sat on a "
                     "crop plant; 9 plants in 10 were found. Of the spots flagged as other green, 19 in 20 were not "
                     "crop plants, and about 7 in 10 of such spots were flagged. Only the counted share of the plot is "
                     "in the number: the rest is ground where single plants cannot be told apart, or thin edges. Other "
                     "green is not named: weed, grass and bush cannot be told apart by this. Not measured on other "
                     "photos or crops. A count of a few rows on the ground is the way to an exact figure."),
        "source": "Ingabe plant finder on the drone photo",
    }


async def _crop_seen(s3: Any, bucket: str, metadata: dict[str, Any], plots: Any, number: int,
                     project_id: Optional[str], scope: str, layer_id: str) -> Optional[str]:
    """The crop in this plot as the cards name it (photo_context.plot_looks): checked on the ground, else named from
    the plot's fingerprint and the checked plots, else by this scope's vision survey; None if none names it."""
    looks = await photo_context.plot_looks(s3, bucket, metadata, plots, project_id, place=None, scope=scope,
                                           start=False, retry_failed=False, layer_id=layer_id)
    look = looks.survey.look(number) if looks.survey else None
    if look is None or look.main_crop == "unsure":
        return None
    return drone_vision.CROP_LABELS[look.main_crop]


async def _checked_by_a_person(s3: Any, bucket: str, photo_key: str, layer_id: str,
                               feature: dict[str, Any]) -> Optional[str]:
    """What a person's own check of the count says for this plot, when the plot is a field they outlined and they
    checked it as its line is now; else None."""
    if not field_taps.enabled():
        return None
    try:
        for row in await field_taps.kept(s3, bucket, photo_key, layer_id):
            if row["number"] == feature["properties"]["number"] and field_facts.same_field(row["geometry"],
                                                                                           feature["geometry"]):
                summary = await count_check.summary(s3, bucket, photo_key, layer_id, row)
                return summary["said"] if summary else None
    except Exception:  # the check is an extra: the count is still given when the check cannot be read
        logger.exception("count check of plot %s could not be read", feature["properties"].get("number"))
    return None


def points_drawn(points: dict[str, Any], show_other_green: bool) -> dict[str, Any]:
    """The points the map is given: the plants alone, or with the other green when the person asked for it."""
    if show_other_green:
        return points
    return {"type": "FeatureCollection",
            "features": [f for f in points["features"] if f["properties"]["kind_code"] == 0]}


plants_about = field_facts.about  # a count as it is said, the same on every surface


CLOSE_VIEW_M = 30.0  # the square the map opens on, inside the plot


def _close_view(feature: dict[str, Any]) -> list[float]:
    """A CLOSE_VIEW_M square around a point inside the plot (WGS84 bounds)."""
    point = shape(feature["geometry"]).representative_point()
    half_lat = CLOSE_VIEW_M / 2 / 111_320
    half_lon = half_lat / max(0.1, math.cos(math.radians(point.y)))
    return [point.x - half_lon, point.y - half_lat, point.x + half_lon, point.y + half_lat]


def _bounds(feature: dict[str, Any]) -> list[float]:
    coords = [c for ring in _rings(feature["geometry"]) for c in ring]
    xs, ys = [c[0] for c in coords], [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def _rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return geometry["coordinates"]
    return [ring for polygon in geometry["coordinates"] for ring in polygon]
