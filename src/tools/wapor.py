import asyncio
from datetime import date

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs


def _parse_iso(s: str):
    """Parse a YYYY-MM-DD string. Empty string → None (means 'no constraint')."""
    if not s or not s.strip():
        return None
    return date.fromisoformat(s)


class GetSoilMoistureArgs(BaseModel):
    latitude: float = Field(..., description="Latitude in decimal degrees, WGS84.")
    longitude: float = Field(..., description="Longitude in decimal degrees, WGS84.")
    date_from: str = Field(
        ...,
        description="Start date in YYYY-MM-DD format. Pass empty string '' for no lower bound.",
    )
    date_to: str = Field(
        ...,
        description="End date in YYYY-MM-DD format. Pass empty string '' for no upper bound.",
    )


class GetEvapotranspirationArgs(BaseModel):
    latitude: float = Field(..., description="Latitude in decimal degrees, WGS84.")
    longitude: float = Field(..., description="Longitude in decimal degrees, WGS84.")
    date_from: str = Field(
        ...,
        description="Start date in YYYY-MM-DD format. Pass empty string '' for no lower bound.",
    )
    date_to: str = Field(
        ...,
        description="End date in YYYY-MM-DD format. Pass empty string '' for no upper bound.",
    )
    include_components: bool = Field(
        ...,
        description="If true, return ET broken down into transpiration + interception + evaporation. If false, return total ET only.",
    )


async def get_soil_moisture(
    args: GetSoilMoistureArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get soil moisture data from WaPOR for a specific location."""
    from src.services.wapor_service import query_soil_moisture

    date_from = _parse_iso(args.date_from)
    date_to = _parse_iso(args.date_to)
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: query_soil_moisture(
            lat=args.latitude, lon=args.longitude,
            date_from=date_from, date_to=date_to,
        ),
    )


async def get_evapotranspiration(
    args: GetEvapotranspirationArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get evapotranspiration data from WaPOR for a specific location."""
    from src.services.wapor_service import query_et

    date_from = _parse_iso(args.date_from)
    date_to = _parse_iso(args.date_to)
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: query_et(
            lat=args.latitude, lon=args.longitude,
            date_from=date_from, date_to=date_to,
            include_components=bool(args.include_components),
        ),
    )
