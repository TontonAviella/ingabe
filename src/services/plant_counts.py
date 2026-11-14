"""Green peaks on a drone photo: every green thing about a crown wide, as one point each.

NOT a plant counter. Colour separates leaf from soil; it cannot tell a crop plant from a weed, a grass tuft or a
bush, which are green too. Used as a count of plants it gave 1,503 where two markers counted 545 crop plants on
16 squares of Cyampirita (Test 80, 2026-11-14), and before a flat top was one point it gave a 697 m² field
4,987 plants. Plants are found by their shape in `plant_finder`; this module finds the green that is left over,
which `plant_finder` flags as "other green".

How: the green pixels (excess green, 2g − r − b, with the threshold set per picture by Otsu's method), smoothed to
about a crown, and the local peaks of that. A peak is one point however many pixels its top has: a patch of solid
green (a bush, weeds, a large crown, plants grown together) has a flat top and is one point, never one per pixel.
"""

from __future__ import annotations

import numpy as np
from scipy import ndimage

SIGMA_M = 0.12  # smoothing: about a young crown's radius
SPACING_M = 0.5  # the window a peak must be highest in: two crowns closer than half of this are one peak
MIN_DENSITY = 0.3  # share of green around a peak for it to be a crown, not a stray leaf
SOLID = 0.999  # smoothed green this full means green all around: the core of a patch wider than a crown


def _otsu(values: np.ndarray) -> float:
    hist, edges = np.histogram(values, bins=256)
    mids = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * mids)
    m0 = s0 / np.maximum(w0, 1)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1)
    return float(mids[np.argmax(w0 * w1 * (m0 - m1) ** 2)])


def crowns(rgb: np.ndarray, inside: np.ndarray, metres_per_px: float) -> tuple[np.ndarray, np.ndarray]:
    """((row, col) of each green peak found in an RGB array, the mask of solid green patches), only where
    `inside` is True. A peak is one point whatever the shape of its top; a solid patch is green wider than a
    crown, where the smoothed green is full."""
    nothing = np.empty((0, 2), dtype=int), np.zeros(inside.shape, dtype=bool)
    pixels = rgb.astype("float32")
    total = pixels.sum(axis=2) + 1e-6
    r, g, b = (pixels[..., i] / total for i in range(3))
    excess_green = 2 * g - r - b
    if inside.sum() < 100:
        return nothing
    green = (excess_green > _otsu(excess_green[inside])) & inside
    green = ndimage.binary_opening(green, iterations=1)
    density = ndimage.gaussian_filter(green.astype("float32"), SIGMA_M / metres_per_px)
    size = max(3, int(round(SPACING_M / metres_per_px)))
    peaks = (density == ndimage.maximum_filter(density, size=size)) & (density > MIN_DENSITY) & inside
    # a top is one peak: on a flat top every pixel equals the maximum, so touching peak pixels are one peak
    tops, count = ndimage.label(peaks, structure=np.ones((3, 3), dtype=int))
    if count == 0:
        return nothing
    found = []
    for rows, cols in ndimage.find_objects(tops):
        own = np.argwhere(tops[rows, cols] > 0)  # the pixels of this top (tops in one box are one top or near it)
        middle = own[np.argmin(((own - own.mean(axis=0)) ** 2).sum(axis=1))]
        found.append((rows.start + middle[0], cols.start + middle[1]))
    patches, _ = ndimage.label(green, structure=np.ones((3, 3), dtype=int))
    solid = np.isin(patches, np.unique(patches[(density >= SOLID) & green])) & green
    return np.array(found, dtype=int), solid
