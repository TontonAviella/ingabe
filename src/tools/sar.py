import asyncio
from typing import Optional

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs


def _parse_bbox(bbox_str: str) -> tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must have 4 values, got {len(parts)}")
    return (parts[0], parts[1], parts[2], parts[3])


def _none_if_empty(s: Optional[str]) -> Optional[str]:
    return s.strip() if s and s.strip() else None


class PredictNdviFromSarArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    target_date: str = Field(
        ...,
        description="Target date YYYY-MM-DD, OR empty string '' to use the most recent SAR scene.",
    )


class DetectWaterBodiesArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    date: str = Field(
        ...,
        description="Date YYYY-MM-DD, OR empty string '' to use the most recent scene.",
    )


class DetectFloodExtentArgs(BaseModel):
    bbox: str = Field(..., description="Bounding box as 'minLon,minLat,maxLon,maxLat'.")
    date_before: str = Field(..., description="Pre-flood date YYYY-MM-DD.")
    date_after: str = Field(..., description="Post-flood date YYYY-MM-DD.")


async def predict_ndvi_from_sar(
    args: PredictNdviFromSarArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Predict NDVI from Sentinel-1 SAR backscatter using ML model."""
    from src.services.sar_ndvi import get_sar_ndvi_predictor

    svc = get_sar_ndvi_predictor()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.predict_ndvi(bbox, _none_if_empty(args.target_date))
    )


async def detect_water_bodies(
    args: DetectWaterBodiesArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Detect water bodies from Sentinel-1 SAR imagery."""
    from src.services.sar_water import get_sar_water_service

    svc = get_sar_water_service()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.detect_water(bbox, _none_if_empty(args.date))
    )


async def detect_flood_extent(
    args: DetectFloodExtentArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Detect flood extent by comparing two SAR images (before/after)."""
    from src.services.sar_water import get_sar_water_service

    svc = get_sar_water_service()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: svc.detect_flood(bbox, args.date_before, args.date_after)
    )
