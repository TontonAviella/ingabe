"""Sage's tools for a drone photo on the map: what the question cards found, and the plants in one plot.

Both read the same facts the cards use (src/services/photo_context.py), so Sage and the cards agree.
"""

from __future__ import annotations

import asyncio
import json
import math
import uuid
from typing import Any, Optional

from pydantic import BaseModel, Field
from shapely.geometry import shape

from src.routes.websocket import kue_ephemeral_action
from src.services import (
    crop_fingerprints,
    drone_cards,
    drone_vision,
    field_checks,
    photo_context,
    photo_plots,
    plant_counts,
)
from src.structures import async_read_conn
from src.tools.geojson_transport import geojson_layer_update
from src.tools.pyd import IngabeToolCallMetaArgs
from src.utils import get_async_s3_client, get_bucket_name

# The cards Sage reads for an overview, in this order: plots, crops, problems, stage, weeds, bare ground.
FINDING_CARDS = ("field_outlines", "crop_types", "plot_problems", "plot_stage", "weeds", "bare_ground", "plots_green")
RECORD_CARDS = ("soil", "history")  # added when the project has a lab report or harvest records
SPOT_COLOUR = "#FF4D1A"  # the cards' colour for things found inside plots

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
        "findings": findings,
        "source": "Ingabe drone photo analysis (the question cards)",
    }


async def count_plants_in_plot(args: CountPlantsArgs, meta: IngabeToolCallMetaArgs) -> dict[str, Any]:
    """Count the plants in one plot of a drone photo and draw each plant as a dot on the map. Gives the count
    with its likely range (±15%), plants per m² and per hectare, and the share of the plot with no plant
    within 1 m (gaps). Needs a photo of 5 cm per pixel or finer and a plot of up to 2 ha. Works best on young
    crops whose leaves do not yet touch (maize 2-6 weeks after emergence); a count is kept, so asking again is
    free.

    WHEN TO USE: "how many plants are in plot 175", "count the maize in this plot", stand counts, gaps in a plot.
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
    count = await plant_counts.load_count(s3, bucket, key, feature)
    if count is None:
        try:
            count = await asyncio.to_thread(plant_counts.count_plants, url, feature)
        except plant_counts.CannotCount as why:
            return {"status": "error", "error": str(why)}
        await plant_counts.save_count(s3, bucket, key, feature, count)
    org_id = meta.session.get_org_id() if meta.session is not None else None
    look = await _crop_seen(s3, bucket, key, plots, args.plot_number, meta.project_id,
                            drone_vision.reference_scope(org_id, meta.user_uuid))
    name = feature["properties"].get("name") or f"Plot {args.plot_number}"
    # The map opens close enough to see each plant under its dot; zooming out shows the whole plot.
    async with kue_ephemeral_action(meta.conversation_id, f"Drawing the {count.plants:,} plants counted in {name}",
                                    bounds=_close_view(feature)) as payload:
        payload.updates["add_geojson_layer"] = geojson_layer_update(
            source_id=f"plants-{args.plot_number}-{uuid.uuid4().hex[:6]}",
            geojson=count.points, name=f"Plants counted in {name}", bounds=_bounds(feature),
            style={"geometry": "point", "stops": [{"max": 1, "color": SPOT_COLOUR}], "stroke_color": "#FFF1E6",
                   "legend": {"title": f"Plants in {name}", "items": [{"label": f"{count.plants:,} plants, one dot each",
                                                                       "color": SPOT_COLOUR}]}})
    return {
        "status": "success",
        "plot": name,
        "crop_seen": look,
        "plants": count.plants,
        "likely_range": [count.low, count.high],
        "plot_area_m2": count.area_m2,
        "plants_per_m2": count.per_m2,
        "plants_per_ha": count.per_ha,
        "gap_share": count.gap_share,
        "photo_cm_per_pixel": count.cm_per_px,
        "drawn_on_map": "one dot per plant counted",
        "how_sure": ("Each dot is a plant crown found on the photo. Checked against counts by eye in three 5 m "
                     "squares of young maize: within about 10%. Where leaves of neighbouring plants touch they "
                     "cannot be split, so give the count with its range. A count of a few rows on the ground "
                     "makes it exact."),
        "source": "Ingabe plant count on the drone photo",
    }


async def _crop_seen(s3: Any, bucket: str, photo_key: str, plots: Any, number: int, project_id: Optional[str],
                     scope: str) -> Optional[str]:
    """The crop in this plot: checked on the ground (this project's checks), else named from the plot's fingerprint
    and the checked plots, else by this scope's vision survey; None if none names it."""
    survey = await drone_vision.load_survey(s3, bucket, drone_vision.survey_key(photo_key, plots, scope))
    checks = await field_checks.load_checks(s3, bucket, project_id, field_checks.plot_set_id(photo_key, plots))
    prints = await crop_fingerprints.load(s3, bucket, crop_fingerprints.store_key(photo_key, plots))
    examples = await crop_fingerprints.load_examples(s3, bucket, project_id, field_checks.plot_set_id(photo_key, plots))
    survey = field_checks.apply(crop_fingerprints.looked(survey, prints, {**examples,
                                                                         **{n: c.crop for n, c in checks.items()}}),
                                checks)
    look = survey.look(number) if survey else None
    if look is None or look.main_crop == "unsure":
        return None
    return drone_vision.CROP_LABELS[look.main_crop]


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
