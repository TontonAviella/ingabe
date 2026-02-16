"""Pydantic models for layer upload and processing pipelines."""

from typing import List, Literal, Optional
from pydantic import BaseModel, Field


class MetadataUpdates(BaseModel):
    original_srid: Optional[int] = None
    feature_count: Optional[int] = None
    raster_value_stats_b1: Optional[dict] = None
    pmtiles_key: Optional[str] = None
    source: Optional[str] = None
    layer_name: Optional[str] = None
    geometry_type: Optional[str] = None


class LayerBoundsMetadata(BaseModel):
    bounds: Optional[List[float]] = None
    geometry_type: str = "unknown"
    feature_count: Optional[int] = None
    metadata_updates: MetadataUpdates = Field(default_factory=MetadataUpdates)


class VectorProcessingResult(BaseModel):
    layer_id: str
    bounds: Optional[List[float]] = None
    geometry_type: str
    feature_count: Optional[int] = None
    metadata: MetadataUpdates
    pmtiles_key: Optional[str] = None
    maplibre_style: Optional[List[dict]] = None
    layer_type: Literal["vector"] = "vector"


class PointCloudPreprocessResult(BaseModel):
    path: str
    bounds: List[float]
    temp_dir: str


class InternalLayerUploadResponse(BaseModel):
    id: str
    name: str
    type: str
    url: str  # Direct URL to the layer
    message: str = "Layer added successfully"
