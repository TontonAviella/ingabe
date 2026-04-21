import asyncio
import logging
from typing import Any, Dict

import requests
from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_NOMINATIM_HEADERS = {"User-Agent": "mundi.ai/1.0 (ntabukiraniroroger@gmail.com)"}


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

    try:
        results = await asyncio.to_thread(_do_geocode)
    except Exception as e:
        logger.exception("Nominatim search failed for %r", query)
        return {"status": "error", "error": f"Geocoding failed: {e}"}

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
