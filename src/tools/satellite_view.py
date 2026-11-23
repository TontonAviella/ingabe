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
    many passes; ground with an older look is marked). Soil moisture: FAO's estimate in 100 m squares for the
    newest ten days. Both are drawn as hexagons that get smaller as the map is zoomed in.

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


# What each part of the answer is read from, for the line of sources under it.
# No brackets: the app shows each source as a chip and drops them (sageReplyText.ts).
_SOURCE_OF = {"plants": "Sentinel-2, 10 m squares", "soil": "FAO WaPOR, 100 m squares"}


def reply(result: dict[str, Any]) -> str:
    """The answer in words, from the tool's own sentences, in the shape of Sage's other answers (AnswerStyle in
    system_prompt.py): one bold line that says the finding, a few short points, the sources.

    Roger, 2026-11-23: the first answer said every sentence the view holds, three dense paragraphs, and lost the
    reader. This one leads with what the two layers say together and keeps, of the rest, what the reader came
    for (how green, which way it is going, how wet) and what must not be missed (an older look, haze, an
    estimate). The days each square was seen and the soil's fuller caveats are on the map's key, on a tap of a
    hexagon, and in the satellite question card, which says every sentence."""
    if result.get("status") != "success":
        return str(result.get("error") or "The satellite view could not be made just now.")
    plants, soil = result.get("plants"), result.get("soil")
    if result.get("together"):
        head = result["together"]
    elif plants:
        head = f"The middle square of {result['photo']} reads NDVI {plants['median']:.2f}."
    else:
        head = f"Soil moisture under {result['photo']} reads {soil['median']:.0f}%." if soil else "Here is what the satellites show."
    points = []
    if plants:
        points.append("- **Plants:** " + plants["short"])
    if soil:
        points.append("- **Soil moisture:** " + soil["short"])
    care = [*(plants["care"] if plants else []), *([soil["care"]] if soil else []), *(result.get("missing") or [])]
    if care:
        points.append("- **Read with care:** " + " ".join(care))
    parts = [f"**{head}**", "\n".join(points)]
    layers = result.get("layers") or []
    taps = {satellite_layers.PLANTS_KIND: "tap a hexagon for its NDVI and its date",
            satellite_layers.SOIL_KIND: "tap a soil moisture figure for its reading"}
    how = " and ".join(taps[layer["kind"]] for layer in layers if layer.get("kind") in taps)
    how = f": {how}." if how else "."
    if len(layers) == 2:
        parts.append(f"Both are on the map, each with its own switch{how}")
    elif layers:
        parts.append(f"The layer \"{layers[0]['name']}\" is on the map{how}")
    if result.get("not_drawn"):
        parts.append(f"I could not draw {' or '.join(result['not_drawn'])} on the map just now; the numbers above still hold.")
    read = [_SOURCE_OF[name] for name, part in (("plants", plants), ("soil", soil)) if part]
    if read:
        parts.append("Sources: " + " · ".join(read))
    return "\n\n".join(parts)
