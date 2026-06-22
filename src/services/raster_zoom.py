import math
import os
from typing import Any, Sequence

WEB_MERCATOR_CIRCUMFERENCE_M = 40075016.68557849


def _clamp_zoom(value: int) -> int:
    return max(0, min(22, value))


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _bounds_extent_m(bounds: Sequence[Any] | None) -> float:
    if not bounds or len(bounds) != 4:
        return 0.0
    try:
        west, south, east, north = [float(v) for v in bounds]
    except (TypeError, ValueError):
        return 0.0

    lon_span = abs(east - west)
    if lon_span > 180.0:
        lon_span = 360.0 - lon_span
    lat_span = abs(north - south)
    center_lat = math.radians((south + north) / 2.0)
    width_m = lon_span * 111_320.0 * max(0.01, math.cos(center_lat))
    height_m = lat_span * 110_574.0
    return max(width_m, height_m)


def raster_source_minzoom(metadata: dict[str, Any] | None, bounds: Sequence[Any] | None) -> int:
    """Compute a practical minzoom for high-resolution user rasters.

    Tiny drone orthos are expensive and visually useless at low zooms: the
    entire COG may occupy only a few screen pixels while forcing GDAL to touch
    broad overview windows. Start requesting tiles once the footprint is large
    enough to inspect.
    """
    forced = _env_int("RASTER_SOURCE_MINZOOM")
    if forced is not None:
        return _clamp_zoom(forced)

    metadata = metadata or {}
    explicit = metadata.get("raster_minzoom")
    if explicit is not None:
        try:
            return _clamp_zoom(int(explicit))
        except (TypeError, ValueError):
            pass

    try:
        max_dim = max(int(metadata.get("width") or 0), int(metadata.get("height") or 0))
    except (TypeError, ValueError):
        max_dim = 0
    min_dim = _env_int("RASTER_MINZOOM_MIN_RASTER_DIM") or 10_000
    if max_dim < min_dim:
        return 0

    extent_m = _bounds_extent_m(bounds)
    if extent_m <= 0.0:
        return 0

    target_pixels = max(1.0, _env_float("RASTER_MINZOOM_TARGET_PIXELS", 96.0))
    denominator = 256.0 * extent_m
    if denominator <= 0.0:
        return 0
    zoom = math.ceil(math.log2((target_pixels * WEB_MERCATOR_CIRCUMFERENCE_M) / denominator))
    cap = _env_int("RASTER_SOURCE_MINZOOM_MAX") or 18
    return max(0, min(cap, _clamp_zoom(zoom)))
