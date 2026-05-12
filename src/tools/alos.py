import asyncio
from typing import Optional

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.sar import _parse_bbox


class GetAlosLBandStatsArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    years: str = Field(
        ...,
        description="Comma-separated year list (e.g. '2020,2021,2022') OR empty string '' for all available years.",
    )


class GetAlosTemporalVariationArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    years: str = Field(
        ...,
        description="Comma-separated year list (e.g. '2020,2021,2022') OR empty string '' for all available years.",
    )


def _parse_years(years_str: Optional[str]) -> Optional[list[int]]:
    if not years_str:
        return None
    return [int(y.strip()) for y in years_str.split(",") if y.strip()]


async def get_alos_l_band_stats(
    args: GetAlosLBandStatsArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get ALOS PALSAR L-band backscatter statistics for an area. When at least one year has tiles, the response includes 'displayable_layers' with the most-recent year's HH backscatter COG URL — pass it to display_layer with style_hint='sar_backscatter_db' to paint the L-band map (forest = bright, water = dark)."""
    from src.services.alos_palsar import get_alos_palsar_service

    svc = get_alos_palsar_service()
    bbox = _parse_bbox(args.bbox)
    years = _parse_years(args.years)
    result = await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.get_l_band_stats(bbox, years)
    )

    # Surface a representative HH COG URL for display_layer dispatch.
    try:
        if isinstance(result, dict):
            year_results = result.get("years") or []
            # Pick the most recent year that has an hh_asset_url
            picks = [
                y for y in year_results
                if isinstance(y, dict) and y.get("status") == "success" and y.get("hh_asset_url")
            ]
            if picks:
                latest = max(picks, key=lambda y: y.get("year", 0))
                bbox_str = f"{bbox[0]},{bbox[1]},{bbox[2]},{bbox[3]}"
                result["displayable_layers"] = [{
                    "asset_url": latest["hh_asset_url"],
                    "style_hint": "sar_backscatter_db",
                    "bbox": bbox_str,
                    "layer_name": f"ALOS L-band HH ({latest['year']})",
                    "band_index": 1,
                }]
                result["display_bbox"] = bbox_str
    except Exception:
        pass

    return result


async def get_alos_temporal_variation(
    args: GetAlosTemporalVariationArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get ALOS PALSAR temporal variation analysis for an area."""
    from src.services.alos_palsar import get_alos_palsar_service

    svc = get_alos_palsar_service()
    bbox = _parse_bbox(args.bbox)
    years = _parse_years(args.years)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.get_temporal_variation(bbox, years)
    )
