"""Axis-aligned boxes (x0, y0, x1, y1), in pixels or map units: how much two of them overlap.

One home for the overlap measure, so candidate de-duplication (raster_object_candidates) and the example finder
(which drops what it finds on top of the user's own marks) measure overlap the same way.
"""

from __future__ import annotations

from typing import Sequence


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two boxes: 0 when they do not overlap, 1 when they are the same box."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    intersection = (ix2 - ix1) * (iy2 - iy1)
    area_a = max((ax2 - ax1) * (ay2 - ay1), 0.0)
    area_b = max((bx2 - bx1) * (by2 - by1), 0.0)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0
