import asyncio
from typing import Any, Dict

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs


def _parse_bbox(bbox_str: str) -> tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must have 4 values, got {len(parts)}")
    return (parts[0], parts[1], parts[2], parts[3])


def _enrich_with_displayable_geojson(
    result: Dict[str, Any],
    bbox: tuple[float, float, float, float],
    style_hint: str,
    title: str,
) -> Dict[str, Any]:
    """Move the result's top-level 'geojson' into a displayable_geojson payload.

    SAR water/flood services return a top-level 'geojson' FeatureCollection. Sage
    needs that shape repackaged as displayable_geojson so it can dispatch
    display_geojson_layer. We pop the raw geojson off the top level to keep LLM
    context lean — the same data lives inside displayable_geojson.geojson.
    """
    if not isinstance(result, dict) or result.get("status") != "success":
        return result
    fc = result.get("geojson")
    if not isinstance(fc, dict) or not fc.get("features"):
        return result
    bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
    result["displayable_geojson"] = {
        "geojson": fc,
        "style_hint": style_hint,
        "title": title,
        "bbox": bbox_str,
    }
    result.pop("geojson", None)
    return result


class DetectFloodExtentArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    date_before: str = Field(..., description="Pre-flood date YYYY-MM-DD.")
    date_after: str = Field(..., description="Post-flood date YYYY-MM-DD.")


async def detect_flood_extent(
    args: DetectFloodExtentArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Detect flood extent by comparing two SAR images (before/after). Returns flood area, fraction, and polygon evidence in 'displayable_geojson' — call display_geojson_layer with style_hint='flood_extent' to paint the new flooded area on the map."""
    from src.services.sar_water import get_sar_water_service

    svc = get_sar_water_service()
    bbox = _parse_bbox(args.bbox)
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.detect_flood(bbox, args.date_before, args.date_after)
    )
    return _enrich_with_displayable_geojson(
        result, bbox,
        style_hint="flood_extent",
        title=f"Flood Extent — {args.date_before} → {args.date_after}",
    )
