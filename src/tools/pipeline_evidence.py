from typing import Any

from pydantic import BaseModel, Field

from src.services.pipeline_evidence import read_pipeline_evidence
from src.tools.pyd import IngabeToolCallMetaArgs


class GetPipelineEvidenceStatusArgs(BaseModel):
    source_category: str | None = Field(
        default=None,
        description="Optional source family to check, such as satellite, weather, raster, upload. Leave empty for all geospatial pipeline evidence.",
    )
    pipeline_family: str | None = Field(
        default=None,
        description="Optional exact pipeline family to check, such as satellite_h3_tiles or satellite_scene_catalog.",
    )
    stale_after_hours: float = Field(
        default=24.0,
        ge=0.1,
        le=720.0,
        description="How old evidence can be before it should be treated as stale.",
    )
    max_items: int = Field(
        default=12,
        ge=1,
        le=50,
        description="Maximum number of latest pipeline evidence records to return.",
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
