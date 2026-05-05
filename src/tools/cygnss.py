import asyncio

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.sar import _enrich_with_displayable_geojson, _parse_bbox


class CheckCygnssAvailabilityArgs(BaseModel):
    bbox: str = Field(
        ...,
        description="Bounding box as 'minLon,minLat,maxLon,maxLat', OR empty string '' to default to all of Rwanda.",
    )


class GetCygnssSoilMoistureArgs(BaseModel):
    lat: float = Field(..., description="Latitude in WGS84 decimal degrees.")
    lon: float = Field(..., description="Longitude in WGS84 decimal degrees.")
    days_back: int = Field(
        ...,
        description="Lookback window in days (typical: 90). Pass 0 to use the default of 90.",
    )
    resolution_km: int = Field(
        ...,
        description="Spatial resolution in km (9 or 36). Pass 0 to use the default of 9.",
    )


class GetCygnssWatermaskArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    date: str = Field(
        ...,
        description="Date YYYY-MM-DD, OR empty string '' to use the most recent product.",
    )
    product: str = Field(
        ...,
        description="CYGNSS product name, OR empty string '' to use the default 'watermask_daily'.",
    )


async def check_cygnss_availability(
    args: CheckCygnssAvailabilityArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Check CYGNSS data availability for a region."""
    from src.services.cygnss import get_cygnss_service, RWANDA_BBOX

    svc = get_cygnss_service()
    bbox = _parse_bbox(args.bbox) if (args.bbox and args.bbox.strip()) else RWANDA_BBOX
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.check_data_availability(bbox)
    )


async def get_cygnss_soil_moisture(
    args: GetCygnssSoilMoistureArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get CYGNSS-derived soil moisture for a location."""
    from src.services.cygnss import get_cygnss_service

    svc = get_cygnss_service()
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: svc.get_soil_moisture(
            lat=args.lat, lon=args.lon,
            days_back=args.days_back if args.days_back > 0 else 90,
            resolution_km=args.resolution_km if args.resolution_km > 0 else 9,
        ),
    )


async def get_cygnss_watermask(
    args: GetCygnssWatermaskArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get CYGNSS L-band water body detection mask for an area. Detects water UNDER vegetation canopy where C-band SAR fails. Returns water polygons in 'displayable_geojson' — call display_geojson_layer with style_hint='water' to paint the detected water on the map."""
    from src.services.cygnss import get_cygnss_service

    svc = get_cygnss_service()
    bbox = _parse_bbox(args.bbox)
    date = args.date.strip() if (args.date and args.date.strip()) else None
    product = args.product.strip() if (args.product and args.product.strip()) else "watermask_daily"
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: svc.get_watermask(
            bbox=bbox, date=date, product=product,
        ),
    )
    scene_date = result.get("date") if isinstance(result, dict) else None
    return _enrich_with_displayable_geojson(
        result, bbox,
        style_hint="water",
        title=f"CYGNSS Watermask — {scene_date or 'recent scene'}",
    )
