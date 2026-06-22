from typing import Any

from pydantic import BaseModel, Field

from src.services.pipeline_evidence import read_pipeline_evidence
from src.tools.pyd import IngabeToolCallMetaArgs


class GetPipelineEvidenceStatusArgs(BaseModel):
    source_category: str | None = Field(
        ...,
        description="Source family to check, such as satellite, weather, raster, upload. Use null for all geospatial pipeline evidence.",
    )
    pipeline_family: str | None = Field(
        ...,
        description="Exact pipeline family to check, such as satellite_h3_tiles or satellite_scene_catalog. Use null for all families.",
    )
    stale_after_hours: float = Field(
        ...,
        ge=0.1,
        le=720.0,
        description="How old evidence can be before it should be treated as stale. Use 24 unless the user asks for another freshness window.",
    )
    max_items: int = Field(
        ...,
        ge=1,
        le=50,
        description="Maximum number of latest pipeline evidence records to return. Use 12 unless the user asks for more or less.",
    )


async def get_pipeline_evidence_status(
    args: GetPipelineEvidenceStatusArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Check local Dagster pipeline evidence before claiming data is fresh.

    Use when the user asks whether satellite, weather, H3, or raster pipeline
    data is actually flowing, or before making freshness/trust claims about
    scheduled geospatial analysis. If no evidence is present, be honest and say
    the app has no local proof yet; use live source tools or cache tables next.
    """
    return read_pipeline_evidence(
        source_category=args.source_category,
        pipeline_family=args.pipeline_family,
        stale_after_hours=args.stale_after_hours,
        max_items=args.max_items,
    )
