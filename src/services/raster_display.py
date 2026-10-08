"""How a stored raster layer is drawn as a picture: which bands are shown, and how one band is coloured.

One home for every place that draws a raster layer, so a drone orthophoto looks the same on the map
tiles (`/api/layer/{id}/{z}/{x}/{y}.png`) as in a rendered picture of the map.
"""

from __future__ import annotations

from typing import Any, Optional

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
