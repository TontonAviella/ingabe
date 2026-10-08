"""Counting the plants in one plot of a drone photo, each plant a point that can be checked on the map.

How: the plot's green pixels (excess green, 2g − r − b, with the threshold set per plot by Otsu's method),
smoothed to about a crown, and the local peaks at least a plant spacing apart. A count is kept per photo
and plot, so asking again costs nothing.

Checked on 2026-10-07 on young maize in Cyampirita (3.2 cm per pixel), in three 5 m squares counted by eye:
the dots sit on plants (about 9 in 10), and the counts came within about 10% of the eye's (23 against about
24; 20 against about 22). Where leaves of neighbouring plants touch, neither the eye nor this method can
split them, so a count is given with ±15%. GPT-6 Luna was asked to point at each plant and was not used:
its points formed a regular grid, many on bare soil. Counting needs a photo at 5 cm per pixel or finer;
the best time is 2-4 weeks after emergence, before leaves touch.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.features import geometry_mask
from rasterio.windows import from_bounds
from scipy import ndimage
from shapely.geometry import shape
from shapely.ops import transform as reproject

SIGMA_M = 0.12  # smoothing: about a young crown's radius
SPACING_M = 0.5  # two plants closer than this are one peak
MIN_DENSITY = 0.3  # share of green around a peak for it to be a plant, not a stray leaf
MAX_CM_PER_PX = 5.0  # coarser photos cannot show single plants
MAX_PLOT_HA = 2.0  # larger plots take too long to read live
UNCERTAINTY = 0.15  # ± share given with every count (see the module note)
GAP_M = 1.0  # ground farther than this from any plant counts as a gap
METHOD = "green crowns v1"
_STORE_PREFIX = "plant_counts/v1"


class CannotCount(ValueError):
    """The photo or plot cannot give a plant count; the message says why, in plain words."""


@dataclass(frozen=True)
class PlantCount:
    plot: int
    plants: int
    low: int
    high: int
    area_m2: float
    per_m2: float
    per_ha: int
    gap_share: float  # share of the plot farther than GAP_M from any plant
    cm_per_px: float
    method: str
    done_at: str
    points: dict[str, Any]  # GeoJSON points, WGS84


def _otsu(values: np.ndarray) -> float:
    hist, edges = np.histogram(values, bins=256)
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * mids)
    m0 = s0 / np.maximum(w0, 1)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1)
    return float(mids[np.argmax(w0 * w1 * (m0 - m1) ** 2)])


def crowns(rgb: np.ndarray, inside: np.ndarray, metres_per_px: float) -> np.ndarray:
    """(row, col) of each plant crown found in an RGB array, only where `inside` is True."""
    pixels = rgb.astype("float32")
    total = pixels.sum(axis=2) + 1e-6
    r, g, b = (pixels[..., i] / total for i in range(3))
    excess_green = 2 * g - r - b
    if inside.sum() < 100:
        return np.empty((0, 2), dtype=int)
    green = (excess_green > _otsu(excess_green[inside])) & inside
    green = ndimage.binary_opening(green, iterations=1)
    density = ndimage.gaussian_filter(green.astype("float32"), SIGMA_M / metres_per_px)
    size = max(3, int(round(SPACING_M / metres_per_px)))
    peaks = (density == ndimage.maximum_filter(density, size=size)) & (density > MIN_DENSITY) & inside
    return np.argwhere(peaks)


def _ground_metres_per_px(ds: Any, lat: float) -> float:
    """A pixel's ground size: Web Mercator metres shrink by cos(latitude) on the ground."""
    size = abs(ds.transform.a)
    return size * math.cos(math.radians(lat)) if ds.crs and ds.crs.to_epsg() == 3857 else size


def count_plants(cog_url: str, feature: dict[str, Any]) -> PlantCount:
    """Count the plants in one plot feature (WGS84, with `number` and `area_ha`); blocking, run it in a thread."""
    props = feature["properties"]
    if props.get("area_ha", 0) > MAX_PLOT_HA:
        raise CannotCount(f"This plot is {props['area_ha']:.1f} ha; plants are counted live in plots up to "
                          f"{MAX_PLOT_HA:.0f} ha. Pick a smaller plot.")
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        to_wgs84 = Transformer.from_crs(ds.crs, "EPSG:4326", always_xy=True).transform
        outline_wgs84 = shape(feature["geometry"])
        metres_per_px = _ground_metres_per_px(ds, outline_wgs84.centroid.y)
        if metres_per_px * 100 > MAX_CM_PER_PX:
            raise CannotCount(f"This photo has {metres_per_px * 100:.0f} cm per pixel; single plants need "
                              f"{MAX_CM_PER_PX:.0f} cm or finer.")
        outline = reproject(to_photo, outline_wgs84)
        window = from_bounds(*outline.bounds, transform=ds.transform)
        rgb = np.transpose(ds.read([1, 2, 3], window=window, boundless=True, fill_value=0), (1, 2, 0))
        transform = ds.window_transform(window)
    inside = ~geometry_mask([outline], out_shape=rgb.shape[:2], transform=transform)
    found = crowns(rgb, inside, metres_per_px)
    xs, ys = rasterio.transform.xy(transform, found[:, 0], found[:, 1]) if len(found) else ([], [])
    lons, lats = to_wgs84(np.asarray(xs), np.asarray(ys)) if len(found) else ([], [])
    area_m2 = float(inside.sum()) * metres_per_px ** 2
    gap_share = 0.0
    if len(found) and inside.any():
        seeds = np.ones(inside.shape, dtype=bool)
        seeds[found[:, 0], found[:, 1]] = False
        distance_m = ndimage.distance_transform_edt(seeds) * metres_per_px
        gap_share = float(((distance_m > GAP_M) & inside).sum() / inside.sum())
    plants = len(found)
    per_m2 = plants / area_m2 if area_m2 else 0.0
    points = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(float(lon), 8), round(float(lat), 8)]},
         "properties": {"plot": props["number"]}} for lon, lat in zip(lons, lats)]}
    return PlantCount(plot=props["number"], plants=plants, low=int(plants * (1 - UNCERTAINTY)),
                      high=int(math.ceil(plants * (1 + UNCERTAINTY))), area_m2=round(area_m2, 1),
                      per_m2=round(per_m2, 2), per_ha=int(round(per_m2 * 10_000, -2)), gap_share=round(gap_share, 3),
                      cm_per_px=round(metres_per_px * 100, 1), method=METHOD,
                      done_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), points=points)


def _store_key(photo_key: str, feature: dict[str, Any]) -> str:
    """One count per photo, plot outline and method."""
    raw = json.dumps([photo_key, feature["geometry"], METHOD], sort_keys=True)
    return f"{_STORE_PREFIX}/{hashlib.sha256(raw.encode()).hexdigest()[:32]}.json"


async def load_count(s3: Any, bucket: str, photo_key: str, feature: dict[str, Any]) -> Optional[PlantCount]:
    try:
        response = await s3.get_object(Bucket=bucket, Key=_store_key(photo_key, feature))
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        return PlantCount(**json.loads(await body.read()))


async def save_count(s3: Any, bucket: str, photo_key: str, feature: dict[str, Any], count: PlantCount) -> None:
    await s3.put_object(Bucket=bucket, Key=_store_key(photo_key, feature), Body=json.dumps(asdict(count)).encode(),
                        ContentType="application/json")
