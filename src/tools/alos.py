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
    """Get ALOS PALSAR L-band backscatter statistics for an area."""
    from src.services.alos_palsar import get_alos_palsar_service

    svc = get_alos_palsar_service()
    bbox = _parse_bbox(args.bbox)
    years = _parse_years(args.years)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.get_l_band_stats(bbox, years)
    )


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
