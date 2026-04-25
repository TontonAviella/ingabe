import asyncio
from typing import Optional

from pydantic import BaseModel

from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.sar import _parse_bbox


class CheckCygnssAvailabilityArgs(BaseModel):
    bbox: Optional[str] = None


class GetCygnssSoilMoistureArgs(BaseModel):
    lat: float
    lon: float
    days_back: Optional[int] = 90
    resolution_km: Optional[int] = 9


class GetCygnssWatermaskArgs(BaseModel):
    bbox: str
    date: Optional[str] = None
    product: Optional[str] = "watermask_daily"


async def check_cygnss_availability(
    args: CheckCygnssAvailabilityArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Check CYGNSS data availability for a region."""
    from src.services.cygnss import get_cygnss_service, RWANDA_BBOX

    svc = get_cygnss_service()
    bbox = _parse_bbox(args.bbox) if args.bbox else RWANDA_BBOX
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: svc.check_data_availability(bbox)
    )


async def get_cygnss_soil_moisture(
    args: GetCygnssSoilMoistureArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get CYGNSS-derived soil moisture for a location."""
    from src.services.cygnss import get_cygnss_service

    svc = get_cygnss_service()
    return await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: svc.get_soil_moisture(
            lat=args.lat, lon=args.lon,
            days_back=args.days_back or 90,
            resolution_km=args.resolution_km or 9,
        ),
    )


async def get_cygnss_watermask(
    args: GetCygnssWatermaskArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get CYGNSS water body detection mask for an area."""
    from src.services.cygnss import get_cygnss_service

    svc = get_cygnss_service()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: svc.get_watermask(
            bbox=bbox, date=args.date,
            product=args.product or "watermask_daily",
        ),
    )
