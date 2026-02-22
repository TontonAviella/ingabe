import logging
import numpy as np
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# ESRI / Impact Observatory 10m Annual Land Use Land Cover (9-class)
# COG tiles on public AWS S3 — Sentinel-2 MGRS grid, UTM projection
_LULC_BASE = "https://io-10m-annual-lulc.s3.us-west-2.amazonaws.com"
_LULC_YEAR = 2024  # Latest available year

# Rwanda spans MGRS zones 35M and 36M
_RWANDA_MGRS_ZONES = ["35M", "36M"]

# ---------------------------------------------------------------------------
# Land cover classes  (pixel value -> label)
# ESRI 9-class v3 model (2017-2024)
# ---------------------------------------------------------------------------
WORLDCOVER_CLASSES = {
    1: "Water",
    2: "Trees",
    4: "Flooded Vegetation",
    5: "Crops",
    7: "Built Area",
    8: "Bare Ground",
    9: "Snow/Ice",
    10: "Clouds",
    11: "Rangeland",
}

# Cropland class value — ESRI uses 5 (ESA WorldCover used 40)
CROPLAND_CLASS = 5

# Alias for convenience
CLASS_NAMES = WORLDCOVER_CLASSES

# ---------------------------------------------------------------------------
# Colormaps — numpy uint8 RGBA lookup tables  (256 entries, indexed by pixel)
# ---------------------------------------------------------------------------
_LULC_COLORS = {
    1: (0, 100, 200, 255),       # Water — blue
    2: (0, 100, 0, 255),         # Trees — dark green
    4: (0, 150, 160, 255),       # Flooded Vegetation — teal
    5: (240, 150, 255, 255),     # Crops — pink
    7: (250, 0, 0, 255),         # Built Area — red
    8: (180, 180, 180, 255),     # Bare Ground — gray
    9: (240, 240, 240, 255),     # Snow/Ice — white
    10: (200, 200, 200, 128),    # Clouds — semi-transparent gray
    11: (255, 187, 34, 255),     # Rangeland — orange
}

_CROPLAND_HIGHLIGHT = (34, 197, 94, 255)   # Tailwind green-500
_CROPLAND_MUTED = (120, 120, 120, 80)      # semi-transparent gray


def _build_lut(mode: str) -> np.ndarray:
    """Build a 256x4 uint8 RGBA lookup table for the given mode."""
    lut = np.zeros((256, 4), dtype=np.uint8)  # default: transparent black

    if mode == "cropland":
        for val in _LULC_COLORS:
            if val == CROPLAND_CLASS:
                lut[val] = _CROPLAND_HIGHLIGHT
            else:
                lut[val] = _CROPLAND_MUTED
    else:  # "all"
        for val, rgba in _LULC_COLORS.items():
            lut[val] = rgba

    return lut


# Pre-built lookup tables — avoid re-creating per tile
LUT_ALL = _build_lut("all")
LUT_CROPLAND = _build_lut("cropland")


def get_lut(mode: str) -> np.ndarray:
    if mode == "cropland":
        return LUT_CROPLAND
    return LUT_ALL


# ---------------------------------------------------------------------------
# Tile URL resolution — ESRI MGRS-based COG tiles
# ---------------------------------------------------------------------------

def get_tile_urls(zones: List[str], year: int = _LULC_YEAR) -> List[str]:
    """Return ESRI LULC COG URLs for the given MGRS zones and year."""
    return [f"{_LULC_BASE}/{zone}_{year}.tif" for zone in zones]


def get_rwanda_tile_urls(year: int = _LULC_YEAR) -> List[str]:
    """ESRI 10m LULC COG URLs for Rwanda (MGRS zones 35M and 36M)."""
    return get_tile_urls(_RWANDA_MGRS_ZONES, year)


def open_rwanda_datasets_warped(year: int = _LULC_YEAR):
    """Open Rwanda LULC COGs as WarpedVRT datasets in EPSG:4326.

    The ESRI tiles are in UTM projection (EPSG:32735/32736).
    WarpedVRT reprojects them lazily to EPSG:4326 so that
    rasterio.merge(bounds=...) works with lon/lat bounding boxes.

    Returns a list of (WarpedVRT, raw_dataset) tuples.
    Caller must close both when done.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.vrt import WarpedVRT

    pairs = []
    for url in get_rwanda_tile_urls(year):
        ds = rasterio.open(url)
        vrt = WarpedVRT(
            ds,
            crs="EPSG:4326",
            resampling=Resampling.nearest,
        )
        pairs.append((vrt, ds))
    return pairs


# ---------------------------------------------------------------------------
# Tile rendering — rasterio WarpedVRT reprojects UTM COG, applies colormap
# ---------------------------------------------------------------------------

def _tile_bounds_4326(x: int, y: int, z: int) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) in EPSG:4326 for an XYZ tile."""
    import math
    n = 1 << z
    lon_min = x / n * 360.0 - 180.0
    lon_max = (x + 1) / n * 360.0 - 180.0
    lat_max = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    lat_min = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return (lon_min, lat_min, lon_max, lat_max)


# Rwanda geographic extent (approximate, with buffer)
_RWANDA_BOUNDS = (28.5, -3.0, 31.2, -0.9)


def render_tile(
    x: int,
    y: int,
    z: int,
    mode: str = "all",
    clip_geometry: Optional[dict] = None,
) -> Optional[bytes]:
    """
    Render a single LULC XYZ tile as a colormapped RGBA PNG.

    Uses rasterio WarpedVRT to lazily reproject the UTM COGs to EPSG:4326,
    then reads the tile window via rasterio.merge and applies the discrete
    land-cover colormap.

    Args:
        clip_geometry: Optional GeoJSON geometry (in EPSG:4326) to clip the
            tile to.  Pixels outside this geometry are set to transparent.
            Used to restrict the layer to a specific district/sector/cell.

    Returns PNG bytes, or None if the tile is outside extent.
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.merge import merge
    from rasterio.vrt import WarpedVRT
    from rasterio.transform import from_bounds
    from PIL import Image
    import io

    TILE_SIZE = 256

    # Compute tile bounds in EPSG:4326
    west, south, east, north = _tile_bounds_4326(x, y, z)

    # Quick check: does this tile intersect Rwanda at all?
    rw, rs, re, rn = _RWANDA_BOUNDS
    if east < rw or west > re or north < rs or south > rn:
        return None

    tile_urls = get_rwanda_tile_urls()
    lut = get_lut(mode)

    # Open COGs via WarpedVRT (UTM → EPSG:4326, nearest-neighbour for
    # categorical data) and merge into the tile window.
    datasets = []
    raw_datasets = []
    try:
        for url in tile_urls:
            ds = rasterio.open(url)
            vrt = WarpedVRT(
                ds,
                crs="EPSG:4326",
                resampling=Resampling.nearest,
            )
            datasets.append(vrt)
            raw_datasets.append(ds)

        # merge reads only the pixels inside `bounds` from all datasets
        mosaic, mosaic_transform = merge(
            datasets,
            bounds=(west, south, east, north),
            res=(
                (east - west) / TILE_SIZE,
                (north - south) / TILE_SIZE,
            ),
            resampling=Resampling.nearest,
            nodata=0,
        )
    except Exception:
        logger.exception("WorldCover merge failed for z=%d x=%d y=%d", z, x, y)
        return None
    finally:
        for vrt in datasets:
            vrt.close()
        for ds in raw_datasets:
            ds.close()

    # mosaic shape: (1, h, w)
    data = mosaic[0]  # (h, w) uint8

    # If tile is entirely nodata, return None
    if not data.any():
        return None

    # Apply discrete colormap via lookup table
    rgba = lut[data]  # (h, w, 4)

    # Nodata pixels (value 0) are already transparent in the LUT

    # -- Admin boundary clipping --
    if clip_geometry is not None:
        try:
            from rasterio.features import geometry_mask

            h, w = rgba.shape[:2]
            transform = from_bounds(west, south, east, north, w, h)

            geom_mask = geometry_mask(
                [clip_geometry],
                out_shape=(h, w),
                transform=transform,
                invert=False,  # True = outside = masked
            )
            rgba[geom_mask, 3] = 0

        except Exception as e:
            logger.warning("Clip geometry masking failed: %s", e)

    # Encode to PNG
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
