"""Preprocessing functions for raster, point cloud, and vector layers."""

import asyncio
import logging
import os
import tempfile
from typing import List, Optional

import fiona
import laspy
from fastapi import HTTPException, status
from osgeo import gdal, osr
from opentelemetry import trace
from pyproj import Transformer

from src.upload.models import (
    LayerBoundsMetadata,
    MetadataUpdates,
    PointCloudPreprocessResult,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


def preprocess_raster(temp_file_path: str, metadata: dict):
    """Extract bounds and statistics from a raster file.

    Mutates ``metadata`` in-place to add EPSG and band statistics.
    Returns bounds as ``[xmin, ymin, xmax, ymax]`` in EPSG:4326, or ``None``.
    """
    bounds = None
    ds = gdal.Open(temp_file_path)
    if ds:
        gt = ds.GetGeoTransform()
        width = ds.RasterXSize
        height = ds.RasterYSize

        xmin = gt[0]
        ymax = gt[3]
        xmax = gt[0] + width * gt[1] + height * gt[2]
        ymin = gt[3] + width * gt[4] + height * gt[5]

        bounds = [xmin, ymin, xmax, ymax]

        src_crs = ds.GetProjection()
        if src_crs:
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(src_crs)
            epsg_code = src_srs.GetAuthorityCode(None)
            if epsg_code:
                metadata["original_srid"] = int(epsg_code)
        else:
            logger.warning(
                "Raster has no CRS — bounds stored as-is and may be incorrect. "
                "Consider assigning a CRS with gdal_warpreproject."
            )
            metadata["crs_missing"] = True

        if src_crs and "EPSG:4326" not in src_crs and "WGS84" not in src_crs:
            src_srs = osr.SpatialReference()
            src_srs.ImportFromWkt(src_crs)
            transformer = Transformer.from_crs(
                src_srs.ExportToProj4(), "EPSG:4326", always_xy=True
            )
            xmin, ymin = transformer.transform(bounds[0], bounds[1])
            xmax, ymax = transformer.transform(bounds[2], bounds[3])

            bounds = [xmin, ymin, xmax, ymax]

        if ds.RasterCount == 1:
            try:
                band = ds.GetRasterBand(1)
                stats = band.ComputeStatistics(False)  # [min, max, mean, stdev]
                min_val, max_val = stats[0], stats[1]
                metadata["raster_value_stats_b1"] = {
                    "min": min_val,
                    "max": max_val,
                }
            except Exception as e:
                logger.warning("Error computing raster statistics: %s", e)
        ds = None

    return bounds


async def preprocess_point_cloud(
    temp_file_path: str, metadata: dict
) -> PointCloudPreprocessResult:
    """Reproject point cloud to EPSG:4326 and extract metadata.

    Mutates ``metadata`` in-place with anchor and z-range.
    Returns a :class:`PointCloudPreprocessResult` with the reprojected file path,
    bounds, and temp directory (caller must clean up).
    """
    with tracer.start_as_current_span("internal_upload_layer.laspy"):
        las = laspy.read(temp_file_path)

        mid_x = (las.header.mins[0] + las.header.maxs[0]) / 2
        mid_y = (las.header.mins[1] + las.header.maxs[1]) / 2

        src_crs = las.header.parse_crs()
        if src_crs is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Point cloud file (.las, .laz) does not have a CRS, which is required to display on the map",
            )

        transformer = Transformer.from_crs(src_crs, 4326, always_xy=True)
        lon, lat = transformer.transform(mid_x, mid_y)
        min_x, min_y, min_z = las.header.mins
        max_x, max_y, max_z = las.header.maxs

    min_lon, min_lat = transformer.transform(min_x, min_y)
    max_lon, max_lat = transformer.transform(max_x, max_y)

    bounds = [min_lon, min_lat, max_lon, max_lat]

    metadata["pointcloud_anchor"] = {"lon": lon, "lat": lat}
    metadata["pointcloud_z_range"] = [min_z, max_z]

    temp_dir = tempfile.mkdtemp()
    auxiliary_temp_file_path = os.path.join(temp_dir, "4326.laz")
    las2las_cmd = [
        "las2las64",
        "-i",
        temp_file_path,
        "-set_version",
        "1.3",
        "-proj_epsg",
        "4326",
        "-o",
        auxiliary_temp_file_path,
    ]

    try:
        with tracer.start_as_current_span("internal_upload_layer.las2las"):
            process = await asyncio.create_subprocess_exec(*las2las_cmd)
            await process.wait()

        if not os.path.exists(auxiliary_temp_file_path):
            raise Exception("las2las did not create output file")
        lasinfo_cmd = ["lasinfo64", auxiliary_temp_file_path]
        lasinfo_process = await asyncio.create_subprocess_exec(
            *lasinfo_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await lasinfo_process.wait()

        if lasinfo_process.returncode != 0:
            raise Exception(
                f"Output file validation failed - lasinfo64 returned exit code {lasinfo_process.returncode}"
            )

    except Exception as e:
        logger.error("Error converting point cloud to EPSG:4326: %s", e)
        raise e

    new_temp_file_path = auxiliary_temp_file_path
    return PointCloudPreprocessResult(
        path=new_temp_file_path, bounds=bounds, temp_dir=temp_dir
    )


async def get_layer_bounds_and_metadata(
    ogr_source: str,
    layer_type: str,
    original_source: Optional[str] = None,
    dataset_layer: Optional[str] = None,
) -> LayerBoundsMetadata:
    """Extract bounds, geometry type, feature count from any OGR/GDAL source.

    Args:
        ogr_source: Path to local file or OGR-compatible URI.
        layer_type: ``'vector'``, ``'raster'``, or ``'point_cloud'``.
        original_source: Optional original URL for error context.
        dataset_layer: Optional sublayer name (e.g. GeoPackage table).

    Returns:
        :class:`LayerBoundsMetadata` with extracted metadata.
    """
    bounds: Optional[List[float]] = None
    geometry_type: str = "unknown"
    feature_count: Optional[int] = None
    metadata_updates = MetadataUpdates()

    try:
        if layer_type == "raster":
            # Use GDAL for raster bounds extraction
            ds = gdal.Open(ogr_source)
            if ds:
                gt = ds.GetGeoTransform()
                width = ds.RasterXSize
                height = ds.RasterYSize

                # Calculate corner coordinates
                xmin = gt[0]
                ymax = gt[3]
                xmax = gt[0] + width * gt[1] + height * gt[2]
                ymin = gt[3] + width * gt[4] + height * gt[5]
                bounds = [xmin, ymin, xmax, ymax]

                # Check CRS and store EPSG code if available
                src_crs = ds.GetProjection()
                if src_crs:
                    src_srs = osr.SpatialReference()
                    src_srs.ImportFromWkt(src_crs)
                    epsg_code = src_srs.GetAuthorityCode(None)
                    if epsg_code:
                        metadata_updates.original_srid = int(epsg_code)

                    # Transform bounds to EPSG:4326 if needed
                    if "EPSG:4326" not in src_crs and "WGS84" not in src_crs:
                        transformer = Transformer.from_crs(
                            src_srs.ExportToProj4(), "EPSG:4326", always_xy=True
                        )
                        xmin, ymin = transformer.transform(bounds[0], bounds[1])
                        xmax, ymax = transformer.transform(bounds[2], bounds[3])
                        bounds = [xmin, ymin, xmax, ymax]

                # Get statistics for single-band rasters
                if ds.RasterCount == 1:
                    try:
                        band = ds.GetRasterBand(1)
                        stats = band.ComputeStatistics(False)  # [min, max, mean, stdev]
                        min_val, max_val = stats[0], stats[1]
                        metadata_updates.raster_value_stats_b1 = {
                            "min": min_val,
                            "max": max_val,
                        }
                    except Exception as e:
                        logger.warning("Error computing raster statistics: %s", e)

                ds = None

        elif layer_type == "vector":
            # Use Fiona for vector bounds and metadata extraction
            open_kwargs = {}
            if dataset_layer is not None:
                open_kwargs["layer"] = dataset_layer

            with fiona.open(ogr_source, **open_kwargs) as collection:
                # Get bounds and feature count
                bounds = list(collection.bounds) if collection.bounds else None
                feature_count = len(collection)
                metadata_updates.feature_count = feature_count

                # Detect geometry type from schema
                if collection.schema and "geometry" in collection.schema:
                    geom_type = collection.schema["geometry"]
                    geometry_type = geom_type.lower() if geom_type else "unknown"

                    # Check first feature for more specific geometry type
                    if feature_count > 0:
                        first_feature = next(iter(collection))
                        if (
                            first_feature
                            and "geometry" in first_feature
                            and "type" in first_feature["geometry"]
                        ):
                            actual_type = first_feature["geometry"]["type"].lower()
                            if actual_type and actual_type != "null":
                                geometry_type = actual_type

                # Store geometry type in metadata if not unknown
                if geometry_type != "unknown":
                    metadata_updates.geometry_type = geometry_type

                # Handle CRS transformation to EPSG:4326
                src_crs = collection.crs
                if src_crs:
                    # Store EPSG code if available
                    if hasattr(src_crs, "to_epsg") and src_crs.to_epsg():
                        metadata_updates.original_srid = src_crs.to_epsg()

                    # Transform bounds if not already EPSG:4326
                    crs_string = src_crs.to_string()
                    if (
                        "EPSG:4326" not in crs_string
                        and "WGS84" not in crs_string
                        and bounds is not None
                    ):
                        transformer = Transformer.from_crs(
                            src_crs, "EPSG:4326", always_xy=True
                        )
                        xmin, ymin = transformer.transform(bounds[0], bounds[1])
                        xmax, ymax = transformer.transform(bounds[2], bounds[3])
                        bounds = [xmin, ymin, xmax, ymax]

        # For point_cloud, we don't extract bounds here (handled elsewhere)

    except Exception as e:
        # Use original source for context if available, otherwise use ogr_source
        source_for_context = original_source or ogr_source

        # For WFS services, bounds extraction failure is common and expected
        if (
            original_source
            and "SERVICE=WFS" in original_source.upper()
            and "REQUEST=GETFEATURE" in original_source.upper()
        ):
            if "Driver was not able to calculate bounds" in str(e):
                logger.info(
                    "WFS service did not provide spatial bounds (this is normal): %s",
                    source_for_context,
                )
            else:
                logger.info(
                    "WFS metadata extraction had minor issues (continuing normally): %s",
                    e,
                )
        else:
            logger.warning(
                "Error extracting layer metadata from %s: %s",
                source_for_context,
                e,
            )
        # Return defaults on error
        pass

    return LayerBoundsMetadata(
        bounds=bounds,
        geometry_type=geometry_type,
        feature_count=feature_count,
        metadata_updates=metadata_updates,
    )
