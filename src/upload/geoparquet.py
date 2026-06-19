"""GeoParquet analytics artifacts for vector uploads."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass

from boto3.s3.transfer import TransferConfig

from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

one_shot_config = TransferConfig(multipart_threshold=5 * 1024**3)


@dataclass(frozen=True)
class GeoParquetArtifact:
    key: str
    size_bytes: int
    compression: str
    crs: str | None
    feature_count: int


def _read_ogr_source(ogr_source: str, dataset_layer: str | None):
    import geopandas as gpd

    read_kwargs = {}
    if dataset_layer is not None:
        read_kwargs["layer"] = dataset_layer

    try:
        return gpd.read_file(ogr_source, engine="pyogrio", **read_kwargs)
    except TypeError:
        return gpd.read_file(ogr_source, **read_kwargs)


def _write_ogr_source_to_geoparquet(
    ogr_source: str,
    output_path: str,
    *,
    dataset_layer: str | None = None,
) -> GeoParquetArtifact:
    gdf = _read_ogr_source(ogr_source, dataset_layer)
    if gdf.empty:
        raise ValueError("cannot write empty vector layer to GeoParquet")

    try:
        geometry = gdf.geometry
    except AttributeError as exc:
        raise ValueError("vector layer has no active geometry column") from exc

    if geometry is None or geometry.name not in gdf.columns:
        raise ValueError("vector layer has no active geometry column")

    gdf = gdf[geometry.notna()].copy()
    if gdf.empty:
        raise ValueError("vector layer has no non-null geometries")

    if gdf.crs is not None and str(gdf.crs).upper() not in {"EPSG:4326", "OGC:CRS84"}:
        gdf = gdf.to_crs("EPSG:4326")

    compression = "zstd"
    try:
        gdf.to_parquet(output_path, index=False, compression=compression)
    except Exception:
        logger.debug("GeoParquet zstd write failed; retrying with default compression", exc_info=True)
        compression = "default"
        gdf.to_parquet(output_path, index=False)

    return GeoParquetArtifact(
        key="",
        size_bytes=os.path.getsize(output_path),
        compression=compression,
        crs=str(gdf.crs) if gdf.crs is not None else None,
        feature_count=len(gdf),
    )


async def generate_geoparquet_from_ogr_source(
    *,
    layer_id: str,
    ogr_source: str,
    user_id: str,
    project_id: str,
    dataset_layer: str | None = None,
) -> GeoParquetArtifact:
    """Convert an OGR-readable vector source to GeoParquet and upload it to S3."""

    with tempfile.TemporaryDirectory() as temp_dir:
        local_output_file = os.path.join(temp_dir, f"{layer_id}.parquet")
        loop = asyncio.get_running_loop()
        artifact = await loop.run_in_executor(
            None,
            lambda: _write_ogr_source_to_geoparquet(
                ogr_source,
                local_output_file,
                dataset_layer=dataset_layer,
            ),
        )

        geoparquet_key = f"geoparquet/{user_id}/{project_id}/{layer_id}.parquet"
        s3 = await get_async_s3_client()
        await s3.upload_file(
            local_output_file,
            get_bucket_name(),
            geoparquet_key,
            Config=one_shot_config,
        )

        return GeoParquetArtifact(
            key=geoparquet_key,
            size_bytes=artifact.size_bytes,
            compression=artifact.compression,
            crs=artifact.crs,
            feature_count=artifact.feature_count,
        )
