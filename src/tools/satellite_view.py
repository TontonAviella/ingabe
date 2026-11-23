"""Sage's tool for the satellite view of a drone photo: plants (NDVI) and soil moisture, on the map and in words.

The measuring is satellite_view's and the drawing satellite_layers'; this handler checks the photo is the
asker's, puts the two layers on the map and hands back the facts. `reply` turns those facts into the answer
the fast path gives without a model, so the same sentences are said either way.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services import satellite_layers, satellite_view
from src.structures import get_async_read_connection
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)

TOOL_NAME = "show_plants_and_soil_from_satellite"
SOURCES = ("Plants: Sentinel-2 satellite, 10 m squares, each square's newest clear look.",
           "Soil moisture: FAO WaPOR v3 estimate, 100 m squares, one value every 10 days.")


class ShowPlantsAndSoilFromSatelliteArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the drone photo (orthophoto raster) whose crops or plants the question is about.",
    )


async def show_plants_and_soil_from_satellite(
    args: ShowPlantsAndSoilFromSatelliteArgs, meta: IngabeToolCallMetaArgs
) -> dict[str, Any]:
    """How the plants on a farm's drone photo are doing, seen from the satellite, with the soil's moisture: puts
    two layers on the map, each with its own switch, and returns what each shows and what they show together.
    Plants: NDVI on every 10 m square of the photo, each square's newest clear look with its date (clouds hide
    many passes; older squares are drawn faded). Soil moisture: FAO's estimate in 100 m squares for the newest
    ten days.

    WHEN TO USE: crop health, plant condition, greenness, NDVI, stress or soil moisture on an uploaded drone
    photo ("are my plants healthy", "show NDVI on my photo", "is the soil wet enough here"), and as the satellite
    half of "how is my crop doing" after get_drone_photo_findings. A colour drone photo has no near-infrared
    band, so its NDVI comes from here.
    WHEN NOT TO USE: what is in each plot, plant counts or plot sizes (get_drone_photo_findings,
    count_plants_in_plot); greenness of a district, sector or cell, or a history over seasons (the NDVI and
    field-health tools); a drone file that already holds NDVI values (find_stress_zones, interpret_raster_health);
    when the user names GRVI or asks for the drone photo's own colours (analyze_rgb_field).
    Say the returned sentences in plain words and add no numbers of your own; keep the dates, and say when part
    of the photo carries an older look."""
    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            "SELECT layer_id, name, type, bounds, owner_uuid FROM map_layers WHERE layer_id = $1", args.layer_id)
    if not row:
        return {"status": "error", "error": f"Layer {args.layer_id} not found."}
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        return {"status": "error", "error": f"Layer {args.layer_id} is not owned by you."}
    if row["type"] != "raster":
        return {"status": "error", "error": f"Layer {args.layer_id} is type '{row['type']}', not a drone photo."}
    if not row["bounds"]:
        return {"status": "error", "error": "This photo has no place on the map yet, so the satellite cannot be matched to it."}
    bounds = [float(v) for v in row["bounds"]]
    name = str(row["name"] or "this photo")

    try:
        seen = await asyncio.to_thread(satellite_view.view, bounds, date.today())
    except Exception:  # noqa: BLE001 - told to the reader in plain words; the cause is in the log
        logger.exception("satellite view failed for layer %s", args.layer_id)
        return {"status": "error", "error": "The satellite view could not be made just now. Nothing was added to the map."}
    said = satellite_view.words(seen)
    if seen.plants is None and seen.soil is None:
        return {"status": "error", "error": " ".join(said["missing"]) or "The satellites hold nothing for this ground yet."}

    layers = await satellite_layers.put_on_map(
        seen, photo_layer_id=args.layer_id, photo_name=name, user_uuid=meta.user_uuid, map_id=meta.map_id,
        project_id=meta.project_id)
    for layer in layers:
        if layer.reused:
            continue
        async with kue_ephemeral_action(meta.conversation_id, f"Adding {layer.name}", layer_id=layer.layer_id,
                                        update_style_json=True, bounds=layer.bounds or bounds):
            await asyncio.sleep(0.2)

    kinds = {layer.kind for layer in layers}
    not_drawn = [what for kind, part, what in (
        (satellite_layers.PLANTS_KIND, seen.plants, "the plants layer"), (satellite_layers.SOIL_KIND, seen.soil, "the soil moisture layer"),
    ) if part is not None and kind not in kinds]
    return {
        "status": "success",
        "photo": name,
        "plants": said["plants"],
        "soil": said["soil"],
        "together": said["together"],
        "missing": said["missing"],
        "layers": [{"layer_id": layer.layer_id, "name": layer.name, "kind": layer.kind, "already_on_map": layer.reused}
                   for layer in layers],
        "not_drawn": not_drawn,
        "sources": list(SOURCES),
    }


def reply(result: dict[str, Any]) -> str:
    """The answer in words, from the tool's own sentences."""
    if result.get("status") != "success":
        return str(result.get("error") or "The satellite view could not be made just now.")
    parts = [f"Here is what the satellites show over {result['photo']}."]
    if result.get("plants"):
        parts.append("**Plants.** " + " ".join(result["plants"]["sentences"]))
    if result.get("soil"):
        parts.append("**Soil moisture.** " + " ".join(result["soil"]["sentences"]))
    if result.get("together"):
        parts.append("**Together.** " + result["together"])
    parts.extend(result.get("missing") or [])
    names = [f"\"{layer['name']}\"" for layer in result.get("layers") or []]
    if len(names) == 2:
        parts.append(f"Two layers are on the map, each with its own switch: {names[1]} and {names[0]}. "
                     "Tap any square for its reading and its date.")
    elif names:
        parts.append(f"The layer {names[0]} is on the map. Tap any square for its reading and its date.")
    if result.get("not_drawn"):
        parts.append(f"I could not draw {' or '.join(result['not_drawn'])} on the map just now; the numbers above still hold.")
    return "\n\n".join(parts)
