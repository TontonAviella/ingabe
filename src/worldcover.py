import math
import logging
import numpy as np
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# ESA WorldCover 2021 v200 — 3x3 degree COG tiles on public AWS S3
_WC_BASE = "https://esa-worldcover.s3.eu-central-1.amazonaws.com/v200/2021/map"
_WC_TILE_FMT = "ESA_WorldCover_10m_2021_v200_{tile}_Map.tif"

# ---------------------------------------------------------------------------
# Land cover classes  (pixel value → label)
# ---------------------------------------------------------------------------
WORLDCOVER_CLASSES = {
    10: "Tree cover",
    20: "Shrubland",
    30: "Grassland",
    40: "Cropland",
    50: "Built-up",
    60: "Bare / sparse vegetation",
    70: "Snow and ice",
    80: "Permanent water bodies",
    90: "Herbaceous wetland",
    95: "Mangroves",
    100: "Moss and lichen",
}

# ---------------------------------------------------------------------------
# Colormaps — numpy uint8 RGBA lookup tables  (256 entries, indexed by pixel)
# ---------------------------------------------------------------------------
_ESA_COLORS = {
    10: (0, 100, 0, 255),       # Tree cover — dark green
    20: (255, 187, 34, 255),    # Shrubland — orange
    30: (255, 255, 76, 255),    # Grassland — yellow
    40: (240, 150, 255, 255),   # Cropland — pink (ESA official)
    50: (250, 0, 0, 255),       # Built-up — red
    60: (180, 180, 180, 255),   # Bare — gray
    70: (240, 240, 240, 255),   # Snow/ice — white
    80: (0, 100, 200, 255),     # Water — blue
    90: (0, 150, 160, 255),     # Wetland — teal
    95: (0, 207, 117, 255),     # Mangroves — green
    100: (250, 230, 160, 255),  # Moss/lichen — pale yellow
}

_CROPLAND_HIGHLIGHT = (34, 197, 94, 255)   # Tailwind green-500
_CROPLAND_MUTED = (120, 120, 120, 80)      # semi-transparent gray


def _build_lut(mode: str) -> np.ndarray:
    """Build a 256×4 uint8 RGBA lookup table for the given mode."""
    lut = np.zeros((256, 4), dtype=np.uint8)  # default: transparent black

    if mode == "cropland":
        for val in _ESA_COLORS:
            if val == 40:
                lut[val] = _CROPLAND_HIGHLIGHT
            else:
                lut[val] = _CROPLAND_MUTED
    else:  # "all"
        for val, rgba in _ESA_COLORS.items():
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
# Tile URL resolution — bbox → list of WorldCover COG URLs
# ---------------------------------------------------------------------------

def _tile_id(lat: int, lon: int) -> str:
    """Convert lower-left corner lat/lon (integers) to tile ID like S03E027."""
    ns = "N" if lat >= 0 else "S"
    ew = "E" if lon >= 0 else "W"
    return f"{ns}{abs(lat):02d}{ew}{abs(lon):03d}"


def _snap_to_grid(val: float, step: int, snap_floor: bool) -> int:
    """Snap a coordinate to the 3-degree grid. Floor for min, ceil for max."""
    if snap_floor:
        return int(math.floor(val / step) * step)
    else:
        return int(math.ceil(val / step) * step)


def get_tile_urls(bbox: Tuple[float, float, float, float]) -> List[str]:
    """Return WorldCover COG URLs covering the given bbox (xmin, ymin, xmax, ymax in EPSG:4326)."""
    xmin, ymin, xmax, ymax = bbox

    # Snap to 3-degree grid (lower-left corners)
    lon_start = _snap_to_grid(xmin, 3, snap_floor=True)
    lon_end = _snap_to_grid(xmax, 3, snap_floor=False)
    lat_start = _snap_to_grid(ymin, 3, snap_floor=True)
    lat_end = _snap_to_grid(ymax, 3, snap_floor=False)

    urls = []
    lat = lat_start
    while lat < lat_end:
        lon = lon_start
        while lon < lon_end:
            tid = _tile_id(lat, lon)
            filename = _WC_TILE_FMT.format(tile=tid)
            urls.append(f"{_WC_BASE}/{filename}")
            lon += 3
        lat += 3

    return urls


def get_rwanda_tile_urls() -> List[str]:
    """Pre-computed tile URLs for Rwanda (lon 28.8-30.9, lat -2.85 to -1.05)."""
    return get_tile_urls((28.8, -2.85, 30.9, -1.05))


# ---------------------------------------------------------------------------
# Tile rendering — rio-tiler reads remote COG, reprojects, applies colormap
# ---------------------------------------------------------------------------

def render_tile(x: int, y: int, z: int, mode: str = "all") -> Optional[bytes]:
    """
    Render a single WorldCover XYZ tile as a colormapped RGBA PNG.

    Uses rio-tiler to read from the remote AWS COG(s), reproject from
    EPSG:4326 to Web Mercator, and apply the discrete land-cover colormap.

    Returns PNG bytes, or None if the tile is outside WorldCover extent.
    """
    from rio_tiler.io import Reader
    from rio_tiler.mosaic import mosaic_reader
    from rio_tiler.errors import TileOutsideBounds, EmptyMosaicError
    from PIL import Image
    import io

    # For Rwanda we know exactly which tiles; for generic use compute from tile bounds
    tile_urls = get_rwanda_tile_urls()

    lut = get_lut(mode)

    def _read_tile(src_url: str):
        with Reader(src_url) as src:
            return src.tile(x, y, z)

    try:
        # mosaic_reader merges data from multiple COGs (handles tile boundaries)
        img, _ = mosaic_reader(tile_urls, _read_tile)
    except (TileOutsideBounds, EmptyMosaicError):
        return None

    # img.data is (bands, height, width) — WorldCover is single-band uint8
    data = img.data[0]  # shape: (256, 256)
    mask = img.mask      # shape: (256, 256) — 255 = valid, 0 = nodata

    # Apply discrete colormap via lookup table
    rgba = lut[data]  # shape: (256, 256, 4)

    # Apply nodata mask — set alpha to 0 where mask is 0
    rgba[mask == 0, 3] = 0

    # Encode to PNG
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue()
