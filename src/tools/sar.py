import asyncio
from typing import Optional

from pydantic import BaseModel, field_validator

from src.tools.pyd import IngabeToolCallMetaArgs


def _parse_bbox(bbox_str: str) -> tuple[float, float, float, float]:
    parts = [float(x.strip()) for x in bbox_str.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must have 4 values, got {len(parts)}")
    return (parts[0], parts[1], parts[2], parts[3])


class PredictNdviFromSarArgs(BaseModel):
    bbox: str
    target_date: Optional[str] = None


class DetectWaterBodiesArgs(BaseModel):
    bbox: str
    date: Optional[str] = None


class DetectFloodExtentArgs(BaseModel):
    bbox: str
    date_before: str
    date_after: str


async def predict_ndvi_from_sar(
    args: PredictNdviFromSarArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Predict NDVI from Sentinel-1 SAR backscatter using ML model."""
    from src.services.sar_ndvi import get_sar_ndvi_predictor

    svc = get_sar_ndvi_predictor()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: svc.predict_ndvi(bbox, args.target_date)
    )


async def detect_water_bodies(
    args: DetectWaterBodiesArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Detect water bodies from Sentinel-1 SAR imagery."""
    from src.services.sar_water import get_sar_water_service

    svc = get_sar_water_service()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: svc.detect_water(bbox, args.date)
    )


async def detect_flood_extent(
    args: DetectFloodExtentArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Detect flood extent by comparing two SAR images (before/after)."""
    from src.services.sar_water import get_sar_water_service

    svc = get_sar_water_service()
    bbox = _parse_bbox(args.bbox)
    return await asyncio.get_event_loop().run_in_executor(
        None, lambda: svc.detect_flood(bbox, args.date_before, args.date_after)
    )
