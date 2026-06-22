from typing import Any

from pydantic import BaseModel, Field

from src.services.spatial_engines import (
    get_spatial_engine_capabilities as get_spatial_engine_capabilities_service,
)
from src.tools.pyd import IngabeToolCallMetaArgs


class GetSpatialEngineCapabilitiesArgs(BaseModel):
    include_rasterd: bool = Field(
        ...,
        description="Whether to check the configured Rust rasterd sidecar health endpoint. Pass true unless the user only asks about non-raster engines.",
    )
    include_geokernel: bool = Field(
        ...,
        description="Whether to check the configured Rust geokernel sidecar health endpoint. Pass true unless the user only asks about raster rendering.",
    )
    include_whitebox: bool = Field(
        ...,
        description="Whether to report WhiteboxTools availability for terrain, hydrology, drone DEM/LiDAR, housing, infrastructure, and environmental analysis.",
    )
    include_tessera: bool = Field(
        ...,
        description="Whether to report TESSERA/GeoTessera availability for satellite embedding memory and H3/admin feature enrichment.",
    )


async def get_spatial_engine_capabilities(
    args: GetSpatialEngineCapabilitiesArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Report which spatial engines are installed and active.

    Use for diagnostic or technical trust-check turns: for example when a
    developer/operator asks which backend is really installed, or when Sage must
    verify an engine before promising that a specific backend powered a result.
    Do not expose engine names to ordinary field users unless they ask; most
    users care about the result, map, evidence, and recommended action.
    """
    return await get_spatial_engine_capabilities_service(
        include_rasterd=args.include_rasterd,
        include_geokernel=args.include_geokernel,
        include_whitebox=args.include_whitebox,
        include_tessera=args.include_tessera,
    )
