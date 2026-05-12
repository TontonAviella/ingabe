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


def _enrich_with_displayable_layer(
    result: dict,
    layer_code: str,
    style_hint: str,
    title_prefix: str,
    lat: float,
    lon: float,
) -> dict:
    """Add displayable_layers + display_bbox to a WaPOR tool result.

    The LLM uses this to pair the numerical answer with a display_layer call,
    so the user sees the spatial pattern around the queried point in addition
    to the time series.
    """
    if "error" in result or result.get("status") == "error":
        return result

    from src.services.wapor_service import _raster_url, get_latest_available_dekad

    dekad = get_latest_available_dekad()
    if not dekad:
        return result

    cog_url = _raster_url(layer_code, dekad)
    half_deg = 0.05  # ~5km box around the queried point
    result["displayable_layers"] = [
        {
            "asset_url": cog_url,
            "style_hint": style_hint,
            "title": f"{title_prefix} — WaPOR {dekad}",
            "band_index": 1,
        }
    ]
    result["display_bbox"] = (
        f"{lon - half_deg},{lat - half_deg},"
        f"{lon + half_deg},{lat + half_deg}"
    )
    return result


async def get_soil_moisture(
    args: GetSoilMoistureArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get soil moisture data from WaPOR for a specific location."""
    from src.services.wapor_service import query_soil_moisture

    date_from = _parse_iso(args.date_from)
    date_to = _parse_iso(args.date_to)
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: query_soil_moisture(
            lat=args.latitude, lon=args.longitude,
            date_from=date_from, date_to=date_to,
        ),
    )
    return _enrich_with_displayable_layer(
        result,
        layer_code="L2-RSM-D",
        style_hint="soil_moisture",
        title_prefix="Soil Moisture",
        lat=args.latitude,
        lon=args.longitude,
    )


async def get_evapotranspiration(
    args: GetEvapotranspirationArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get evapotranspiration data from WaPOR for a specific location."""
    from src.services.wapor_service import query_et

    date_from = _parse_iso(args.date_from)
    date_to = _parse_iso(args.date_to)
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: query_et(
            lat=args.latitude, lon=args.longitude,
            date_from=date_from, date_to=date_to,
            include_components=bool(args.include_components),
        ),
    )
    return _enrich_with_displayable_layer(
        result,
        layer_code="L2-AETI-D",
        style_hint="evapotranspiration",
        title_prefix="Evapotranspiration",
        lat=args.latitude,
        lon=args.longitude,
    )
