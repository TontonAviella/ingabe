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


async def get_spatial_engine_capabilities(
    args: GetSpatialEngineCapabilitiesArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Report which spatial engines are installed and active.

    Use when the user asks whether Sphere, Forge3D, rasterd, geokernel, or
    browser 3D map rendering is actually installed/active. This prevents Sage/Hermes from
    claiming a renderer or model is powering a result when it is only available
    experimentally or not installed in the current runtime.
    """
    return await get_spatial_engine_capabilities_service(
        include_rasterd=args.include_rasterd,
        include_geokernel=args.include_geokernel,
    )
