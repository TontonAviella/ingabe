"""Sage's tools for a photo inspection of telecom towers on the map: what a person counted on it
(equipment and defects), and which photos still need checking. An adapter only: they read the same facts as the
inspection cards (src/services/tower_cards.py), so Sage and the cards agree, and none ever counts from a picture. The
tools serve Telecom projects while TOWERS is on (industry.available).
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.dependencies.redis_client import get_async_redis
from src.services import industry, photo_inspections, tower_cards, tower_inspection
from src.structures import async_read_conn
from src.tools.pyd import IngabeToolCallMetaArgs
from src.utils import get_async_s3_client, get_bucket_name

_INSPECTION_ON_MAP_SQL = """
SELECT l.layer_id, l.name, l.metadata
  FROM map_layers l
  JOIN user_mundiai_maps m ON m.id = $2 AND l.layer_id = ANY(m.layers)
 WHERE l.layer_id = $1 AND l.type = 'vector'
"""


class TowerInspectionArgs(BaseModel):
    layer_id: str = Field(..., description="The layer_id of the photo inspection: the layer of camera points the "
                                           "photos were uploaded as.")


async def _here(layer_id: str, meta: IngabeToolCallMetaArgs,
                capability: str = "get_tower_inspection_findings") -> tuple[Optional[tower_cards.Here], Optional[str]]:
    """(the cards' facts, None) for a photo inspection on this chat's map, else (None, why not). `capability` is the
    calling tool's: it decides the industry the inspection must belong to."""
    async with async_read_conn("sage_tower_inspection", user_id=meta.user_uuid) as conn:
        row = await conn.fetchrow(_INSPECTION_ON_MAP_SQL, layer_id, meta.map_id)
        if row is None:
            return None, f"No photo inspection {layer_id} on this map."
        metadata = json.loads(row["metadata"]) if isinstance(row["metadata"], str) else dict(row["metadata"] or {})
        prefix = photo_inspections.stored_prefix(metadata, layer_id)
        if prefix is None:
            return None, f"{row['name']} is not a photo inspection (a ZIP of photos uploaded as camera points)."
        # The chat's own project (its map shows the layer), never the last edited map's: a layer added to another
        # project's map keeps each project's work apart, as every Sage tool keys on meta.project_id.
        project_id = meta.project_id
        # The tool's own capability (its industry): photo marking also serves Agriculture, these counts do not.
        project_industry = await industry.industry_of_project(conn, project_id)
        if not industry.available(capability, project_industry):
            return None, "Tower work is for Telecom Towers projects only."
        s3, bucket = await get_async_s3_client(), get_bucket_name()
        photos = await tower_inspection.inspection_photos(s3, bucket, prefix)
        if photos is None:
            return None, ("This inspection was uploaded before its photo facts were kept; upload the photos again to "
                          "work on them.")
        here = await tower_cards.load_here(conn, get_async_redis(), s3, bucket, project_id, layer_id, row["name"],
                                           photos, project_industry or "telecom")
    return here, None


async def get_tower_inspection_findings(args: TowerInspectionArgs, meta: IngabeToolCallMetaArgs) -> dict[str, Any]:
    """What is on the tower according to a photo inspection: for dishes, antennas and radio units, the number a
    person counted (at least N: the most counted on one photo, after checking every suggestion of the finder on it),
    how many photos are counted, suggestions still waiting for a person, and where the photos were taken from
    (heights, camera tilt, directions, photos without GPS). When nothing is checked yet it says so and what to do.

    Also the defects a person counted: missing and loose bolts, with the photos they are on.

    WHEN TO USE: "what equipment is on this tower", "how many antennas / dishes / radio units (RRUs)", "is the
    inventory done", "where were the photos taken from", "are bolts missing or loose", "is the steel safe".
    WHEN NOT TO USE: rust, cracks or other damage (not measured); a raster photo that is not a photo inspection.
    Never count equipment or defects from a picture yourself: give only the counts this tool returns, and never say
    there are no defects where nobody marked them."""
    here, why_not = await _here(args.layer_id, meta)
    if here is None:
        return {"status": "error", "error": why_not}
    return {"status": "success", **tower_cards.findings(here),
            "source": "Ingabe tower inspection: counts checked by a person, photo facts from EXIF and DJI data"}


async def tower_photos_needing_review(args: TowerInspectionArgs, meta: IngabeToolCallMetaArgs) -> dict[str, Any]:
    """The photos of a tower inspection a person still has to check, in the order to work on them: suggestions of
    the finder waiting for review (most first), photos marked but not searched yet, photos the finder is working on,
    then photos with nothing marked (in the order they were taken). A photo's count exists only once it is checked.

    WHEN TO USE: "which photos still need checking", "what is left to review", "where do I start".
    WHEN NOT TO USE: the equipment counts themselves (use get_tower_inspection_findings)."""
    here, why_not = await _here(args.layer_id, meta)
    if here is None:
        return {"status": "error", "error": why_not}
    return {"status": "success", **tower_cards.photos_needing_review(here),
            "source": "Ingabe tower inspection: the work saved on each photo"}
