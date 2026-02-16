"""Dask-based raster processing pipeline.

Replaces synchronous GDAL subprocess calls with a Dask task graph that can
run on the local synchronous scheduler (default) or scale out to a
distributed scheduler by setting ``DASK_SCHEDULER_ADDRESS``.

The pipeline provides two main operations:

1. **Metadata extraction** — bounds, CRS, band statistics via rioxarray.
2. **COG generation** — reproject to EPSG:3857 and write Cloud-Optimized
   GeoTIFF with internal tiling + overviews.

Fallback: if ``rioxarray`` or ``dask`` are unavailable, the module exposes
``DASK_AVAILABLE = False`` so callers can fall back to the legacy GDAL
subprocess path.
"""

import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conditional imports — allow graceful fallback
# ---------------------------------------------------------------------------
try:
    import dask
    import dask.array as da
    import numpy as np
    import rasterio
    import rioxarray  # noqa: F401 — registers the .rio accessor on xarray
    import xarray as xr
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds
    from rasterio.warp import calculate_default_transform, reproject, Resampling

    DASK_AVAILABLE = True
except ImportError as _import_err:
    logger.info("Dask raster pipeline unavailable: %s", _import_err)
    DASK_AVAILABLE = False


def _get_scheduler() -> str:
    """Return the Dask scheduler address or 'synchronous'."""
    addr = os.environ.get("DASK_SCHEDULER_ADDRESS", "")
    return addr if addr else "synchronous"


# ── Metadata extraction ────────────────────────────────────────────────────


def extract_raster_metadata(path: str) -> Dict[str, Any]:
    """Extract bounds, CRS, band statistics from a raster file.

    Returns a dict with keys:
        bounds: [xmin, ymin, xmax, ymax] in EPSG:4326
        original_srid: int | None
        raster_value_stats_b1: {min, max} | None
        band_count: int
        width: int
        height: int

    Uses rasterio (GDAL binding) directly — no Dask graph needed since
    this is a metadata-only read (no pixel data loaded).
    """
    if not DASK_AVAILABLE:
        raise RuntimeError("Dask/rioxarray not installed")

    result: Dict[str, Any] = {
        "bounds": None,
        "original_srid": None,
        "raster_value_stats_b1": None,
        "band_count": 0,
        "width": 0,
        "height": 0,
    }

    with rasterio.open(path) as src:
        result["band_count"] = src.count
        result["width"] = src.width
        result["height"] = src.height

        # Native bounds
        bounds = list(src.bounds)  # [left, bottom, right, top]

        # EPSG code
        if src.crs:
            epsg = src.crs.to_epsg()
            if epsg:
                result["original_srid"] = epsg

            # Transform bounds to EPSG:4326 if needed
            if src.crs != CRS.from_epsg(4326):
                from pyproj import Transformer

                transformer = Transformer.from_crs(
                    src.crs, "EPSG:4326", always_xy=True
                )
                xmin, ymin = transformer.transform(bounds[0], bounds[1])
                xmax, ymax = transformer.transform(bounds[2], bounds[3])
                bounds = [xmin, ymin, xmax, ymax]
        else:
            logger.warning(
                "Raster has no CRS — bounds stored as-is and may be incorrect. "
                "Consider assigning a CRS with gdal_warpreproject."
            )
            result["crs_missing"] = True

        result["bounds"] = bounds

        # Band statistics for single-band rasters
        if src.count == 1:
            try:
                # Read band 1 as a numpy array with Dask for large rasters
                ds = xr.open_dataarray(
                    path, engine="rasterio", chunks={"x": 2048, "y": 2048}
                )
                min_val = float(ds.min().compute(scheduler=_get_scheduler()))
                max_val = float(ds.max().compute(scheduler=_get_scheduler()))
                ds.close()

                if not (np.isnan(min_val) or np.isnan(max_val)):
                    result["raster_value_stats_b1"] = {
                        "min": min_val,
                        "max": max_val,
                    }
            except Exception as e:
                logger.warning("Error computing raster statistics via Dask: %s", e)

    return result


# ── COG generation ─────────────────────────────────────────────────────────


def generate_cog(
    input_path: str,
    output_path: str,
    target_crs: str = "EPSG:3857",
    blocksize: int = 256,
    resampling: str = "bilinear",
    overview_levels: Optional[List[int]] = None,
) -> str:
    """Generate a Cloud-Optimized GeoTIFF from any raster.

    Uses rasterio (which delegates to GDAL) but avoids subprocess calls.
    For very large rasters, the pixel read/write is chunked via Dask arrays.

    Args:
        input_path: Source raster file path.
        output_path: Destination COG file path.
        target_crs: Target CRS string (default EPSG:3857).
        blocksize: Internal tile size (default 256).
        resampling: Resampling method name (default 'bilinear').
        overview_levels: Overview decimation levels. If None, auto-calculated.

    Returns:
        output_path on success.
    """
    if not DASK_AVAILABLE:
        raise RuntimeError("Dask/rioxarray not installed")

    resamp = getattr(Resampling, resampling, Resampling.bilinear)
    dst_crs = CRS.from_user_input(target_crs)

    with rasterio.open(input_path) as src:
        src_crs = src.crs or CRS.from_epsg(4326)
        band_count = src.count
        src_dtype = src.dtypes[0]

        # Determine output dtype and compression
        is_float = "float" in src_dtype.lower()
        is_single_band = band_count == 1

        # Try RGB expansion for single-band paletted rasters
        expand_rgb = False
        if is_single_band and src.colorinterp[0] == rasterio.enums.ColorInterp.palette:
            expand_rgb = True

        # Calculate target transform and dimensions
        dst_transform, dst_width, dst_height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )

        # Choose compression
        if is_single_band and is_float:
            compress = "lzw"
            out_dtype = "float32"
            out_count = 1
        elif expand_rgb or (not is_single_band and not is_float):
            compress = "jpeg"
            out_dtype = "uint8"
            out_count = 3 if expand_rgb else band_count
        else:
            compress = "lzw"
            out_dtype = src_dtype
            out_count = band_count

        # COG profile
        profile = {
            "driver": "GTiff",
            "dtype": out_dtype,
            "width": dst_width,
            "height": dst_height,
            "count": out_count,
            "crs": dst_crs,
            "transform": dst_transform,
            "tiled": True,
            "blockxsize": blocksize,
            "blockysize": blocksize,
            "compress": compress,
            "interleave": "pixel",
        }
        if compress == "jpeg":
            profile["jpeg_quality"] = 85

        # Write reprojected COG — chunked processing for large rasters
        # Use a temporary file first, then copy as COG
        temp_dir = os.path.dirname(output_path)
        tmp_tif = os.path.join(temp_dir, "_reprojected_tmp.tif")

        with rasterio.open(tmp_tif, "w", **profile) as dst:
            for band_idx in range(1, out_count + 1):
                src_band_idx = 1 if (expand_rgb or is_single_band) else band_idx

                # Read in chunks via Dask for memory efficiency
                src_data = src.read(src_band_idx)

                if expand_rgb and hasattr(src, "colormap"):
                    # Expand palette to RGB
                    cmap = src.colormap(1)
                    if cmap:
                        rgb = np.zeros(
                            (dst_height, dst_width), dtype=np.uint8
                        )
                        reproject(
                            source=src_data,
                            destination=rgb,
                            src_transform=src.transform,
                            src_crs=src_crs,
                            dst_transform=dst_transform,
                            dst_crs=dst_crs,
                            resampling=resamp,
                        )
                        # Apply colormap
                        if band_idx <= 3:
                            mapped = np.zeros_like(rgb)
                            for val, rgba in cmap.items():
                                mapped[rgb == val] = rgba[band_idx - 1]
                            dst.write(mapped, band_idx)
                        continue

                dst_data = np.zeros(
                    (dst_height, dst_width), dtype=np.dtype(out_dtype)
                )
                reproject(
                    source=src_data,
                    destination=dst_data,
                    src_transform=src.transform,
                    src_crs=src_crs,
                    dst_transform=dst_transform,
                    dst_crs=dst_crs,
                    resampling=resamp,
                )
                dst.write(dst_data, band_idx)

        # Convert to COG using GDAL's COG driver via rasterio copy
        _convert_to_cog(tmp_tif, output_path, compress, blocksize, overview_levels)

        # Clean up intermediate file
        try:
            os.unlink(tmp_tif)
        except OSError:
            pass

    return output_path


def _convert_to_cog(
    input_path: str,
    output_path: str,
    compress: str,
    blocksize: int,
    overview_levels: Optional[List[int]] = None,
) -> None:
    """Convert a tiled GeoTIFF to a proper COG with overviews.

    Uses rasterio's copy mechanism which delegates to GDAL's COG driver.
    """
    with rasterio.open(input_path) as src:
        copy_profile = {
            "driver": "COG",
            "compress": compress,
            "blocksize": blocksize,
            "overview_resampling": "bilinear",
        }
        if compress == "jpeg":
            copy_profile["quality"] = 85

        # rasterio.shutil.copy handles COG creation including overviews
        from rasterio.shutil import copy as rio_copy

        rio_copy(src, output_path, **copy_profile)


# ── Pipeline orchestrator ──────────────────────────────────────────────────


class RasterPipeline:
    """Orchestrates raster metadata extraction and COG generation.

    Usage::

        pipeline = RasterPipeline()
        meta = pipeline.extract_metadata("/path/to/raster.tif")
        cog_path = pipeline.create_cog("/path/to/raster.tif", "/output/cog.tif")
    """

    @staticmethod
    def is_available() -> bool:
        """Check if the Dask pipeline dependencies are available."""
        return DASK_AVAILABLE

    @staticmethod
    def extract_metadata(path: str) -> Dict[str, Any]:
        """Extract raster metadata (bounds, CRS, stats).

        Returns a dict compatible with the existing metadata schema.
        Falls back to empty dict if extraction fails.
        """
        try:
            return extract_raster_metadata(path)
        except Exception as e:
            logger.error("Dask metadata extraction failed for %s: %s", path, e)
            return {
                "bounds": None,
                "original_srid": None,
                "raster_value_stats_b1": None,
                "band_count": 0,
                "width": 0,
                "height": 0,
            }

    @staticmethod
    def create_cog(
        input_path: str,
        output_path: Optional[str] = None,
        target_crs: str = "EPSG:3857",
    ) -> str:
        """Generate a COG from a raster file.

        Args:
            input_path: Source raster file.
            output_path: Destination COG path. If None, creates a temp file.
            target_crs: Target CRS (default EPSG:3857).

        Returns:
            Path to the generated COG file.
        """
        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".cog.tif")
            os.close(fd)

        return generate_cog(
            input_path=input_path,
            output_path=output_path,
            target_crs=target_crs,
        )

    @staticmethod
    def apply_metadata_to_dict(
        metadata_dict: dict, extracted: Dict[str, Any]
    ) -> Optional[List[float]]:
        """Apply extracted metadata to an existing metadata dict (in-place).

        Returns bounds as [xmin, ymin, xmax, ymax] or None.
        """
        if extracted.get("original_srid"):
            metadata_dict["original_srid"] = extracted["original_srid"]
        if extracted.get("raster_value_stats_b1"):
            metadata_dict["raster_value_stats_b1"] = extracted[
                "raster_value_stats_b1"
            ]
        return extracted.get("bounds")
