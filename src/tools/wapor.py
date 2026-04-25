import asyncio
from datetime import date
from typing import Optional

from pydantic import BaseModel

from src.tools.pyd import IngabeToolCallMetaArgs


class GetSoilMoistureArgs(BaseModel):
    latitude: float
    longitude: float
    date_from: Optional[str] = None
    date_to: Optional[str] = None


class GetEvapotranspirationArgs(BaseModel):
    latitude: float
    longitude: float
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    include_components: Optional[bool] = False


async def get_soil_moisture(
    args: GetSoilMoistureArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get soil moisture data from WaPOR for a specific location."""
    from src.services.wapor_service import query_soil_moisture

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None
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

    date_from = date.fromisoformat(args.date_from) if args.date_from else None
    date_to = date.fromisoformat(args.date_to) if args.date_to else None
    return await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: query_et(
            lat=args.latitude, lon=args.longitude,
            date_from=date_from, date_to=date_to,
            include_components=bool(args.include_components),
        ),
    )
