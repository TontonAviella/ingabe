"""How a stored raster layer is drawn as a picture: which bands are shown, and how one band is coloured.

One home for every place that draws a raster layer, so a drone orthophoto looks the same on the map
tiles (`/api/layer/{id}/{z}/{x}/{y}.png`) as in a rendered picture of the map.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.services import raster_process
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

logger = logging.getLogger(__name__)

# Single-band rasters with value statistics are stretched over their min..max and coloured with this.
SINGLE_BAND_COLORMAP = "spectral_r"


def display_indexes(metadata: Optional[dict]) -> Optional[tuple[int, ...]]:
    """The bands to read for display, or None for all of them.

    The PNG driver caps at 4 bands (RGBA). rio-tiler appends an implicit mask, so a 4-band drone
    ortho becomes 5 bands at encode and CPLE_NotSupportedError fires. Any raster with more than 3
    bands is read as its first 3; the mask becomes the alpha channel.
    """
    band_count = (metadata or {}).get("band_count")
    if isinstance(band_count, int) and band_count > 3:
        return (1, 2, 3)
    return None


def render_png(img: Any, metadata: Optional[dict]) -> bytes:
    """Encode a rio-tiler ImageData read with `display_indexes` as the PNG the map shows."""
    metadata = metadata or {}
    if "raster_value_stats_b1" in metadata:
        from rio_tiler.colormap import cmap  # lazy: rio-tiler loads GDAL; app startup stays light

        min_val = metadata["raster_value_stats_b1"]["min"]
        max_val = metadata["raster_value_stats_b1"]["max"]
        img.rescale(in_range=((min_val, max_val),), out_range=((0, 255),))
        return img.render(img_format="PNG", colormap=cmap.get(SINGLE_BAND_COLORMAP))
    return img.render(img_format="PNG")


def _preview_png_raw(
    cog_url: str,
    metadata: Optional[dict],
    bounds: tuple[float, float, float, float],
    max_size: int,
    env: dict[str, str],
) -> tuple[str, Any]:
    """In a raster worker: ("png", bytes) of `bounds` in Web Mercator, ("outside", None) or ("error", message)."""
    import rasterio
    from rio_tiler.errors import TileOutsideBounds
    from rio_tiler.io import Reader

    try:
        with rasterio.Env(**env), Reader(cog_url) as src:
            img = src.part(
                bounds,
                bounds_crs="EPSG:4326",
                dst_crs="EPSG:3857",
                max_size=max_size,
                indexes=display_indexes(metadata),
            )
            return "png", render_png(img, metadata)
    except TileOutsideBounds:
        return "outside", None
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"


async def preview_png(
    cog_url: str,
    metadata: Optional[dict],
    bounds: tuple[float, float, float, float],
    max_size: int,
) -> Optional[bytes]:
    """The raster inside WGS84 `bounds` as a Web Mercator PNG at most `max_size` pixels a side, as the map shows it.

    None when the raster could not be read (logged with the reason) or does not cover `bounds`.
    """
    try:
        kind, got = await asyncio.to_thread(
            raster_process.run, _preview_png_raw, cog_url, metadata, bounds, max_size, GDAL_HTTP_TIMEOUTS,
        )
    except Exception as e:  # the worker died or never answered
        kind, got = "error", f"raster worker: {e!r}"
    if kind == "error":
        logger.warning("Raster preview read failed for %s: %s", cog_url.split("?")[0], got)
        return None
    return got
