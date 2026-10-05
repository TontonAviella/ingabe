"""GRVI, the greenness index an RGB photo allows: one place for its formula and verdict bands.

GRVI = (Green - Red) / (Green + Red). Without a near-infrared band it is the
best vegetation signal an RGB drone orthophoto gives, roughly 70% as
informative as true NDVI. Values typically:
  -0.05 to +0.05  bare/dry
  +0.05 to +0.15  moderate canopy
  +0.15 to +0.30  healthy green canopy
  > +0.30         dense lush vegetation (or wet leaves at saturation)
These are coarser than NDVI ranges; GRVI saturates earlier than NDVI.
"""

from __future__ import annotations

from typing import Any

import numpy as np

# (lower bound, level, plain sentence), highest first.
GRVI_VERDICT_BANDS = [
    (0.20, "lush_canopy", "Dense, vigorous green canopy."),
    (0.10, "healthy_canopy", "Healthy green canopy. Looks normal for an established crop."),
    (0.03, "moderate_canopy", "Moderate canopy. Could be early growth, partially senescent, or under stress."),
    (-0.05, "sparse_or_stressed", "Sparse vegetation or stress signature. Worth inspecting."),
    (-1.0, "bare_or_dry", "Mostly bare soil or dry/dormant vegetation."),
]

# Below this a pixel is "low green": sparse, stressed or bare (the moderate_canopy lower bound).
LOW_GREEN = 0.03


def grvi_verdict(mean: float) -> tuple[str, str]:
    """(level, sentence) for a GRVI mean."""
    for threshold, level, message in GRVI_VERDICT_BANDS:
        if mean >= threshold:
            return level, message
    return GRVI_VERDICT_BANDS[-1][1], GRVI_VERDICT_BANDS[-1][2]


def grvi(red: Any, green: Any) -> Any:
    """GRVI per pixel as a float array; NaN where red + green is 0 or either band is masked."""
    r = np.ma.filled(np.ma.asarray(red, dtype="float32"), np.nan)
    g = np.ma.filled(np.ma.asarray(green, dtype="float32"), np.nan)
    denom = g + r
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(denom > 0, (g - r) / denom, np.nan)
