import asyncio
import logging
import time
from typing import Any, Dict

import requests
from pydantic import BaseModel, Field

from src.services import disk_memo
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "mundi.ai/1.0 (contact@ingabe.rw)"}
# OpenStreetMap's Nominatim asks every user to keep its answers rather than ask again. Places barely move, so an
# answer is kept on disk for 30 days, then asked again in case the map was corrected.
_KEEP_S = 30 * 24 * 3600


class SearchLocationArgs(BaseModel):
    query: str = Field(
        ...,
        description="Place name to search for, e.g. 'Musanze district, Rwanda' or 'Kigali'",
    )


async def search_location(
    args: SearchLocationArgs, meta: IngabeToolCallMetaArgs
) -> Dict[str, Any]:
    """Search for a location by name and return its bounding box and coordinates. Use this when the user mentions a place name and you need geographic coordinates for satellite imagery tools."""
    query = args.query

    def _do_geocode():
        resp = requests.get(
            _NOMINATIM_URL,
            params={
                "q": query,
                "format": "json",
                "limit": 1,
                "addressdetails": 1,
                "polygon_geojson": 0,
            },
            headers=_NOMINATIM_HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    key = " ".join(query.lower().split())
    found, kept = await asyncio.to_thread(disk_memo.get, "nominatim_search", key)  # sqlite may wait for a lock
    if found and time.time() - kept["at"] < _KEEP_S:
        results = kept["results"]
    else:
        try:
            results = await asyncio.to_thread(_do_geocode)
        except Exception as e:
            logger.exception("Nominatim search failed for %r", query)
            return {"status": "error", "error": f"Geocoding failed: {e}"}
        if results:
            await asyncio.to_thread(disk_memo.put, "nominatim_search", key, {"at": time.time(), "results": results})

    if not results:
        return {"status": "error", "error": f"No results for '{query}'"}

    place = results[0]
    bbox_raw = place.get("boundingbox", [])

    if len(bbox_raw) == 4:
        south, north, west, east = [float(x) for x in bbox_raw]
        bbox = [west, south, east, north]
    else:
        lat = float(place["lat"])
        lon = float(place["lon"])
        bbox = [lon - 0.05, lat - 0.05, lon + 0.05, lat + 0.05]

    return {
        "status": "success",
        "name": place.get("display_name", query),
        "bbox": bbox,
        "latitude": float(place["lat"]),
        "longitude": float(place["lon"]),
        "osm_type": place.get("type"),
    }
