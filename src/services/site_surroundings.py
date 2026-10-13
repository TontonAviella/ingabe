"""What grows around a site, from the inspection's photos looking straight down: trees (SAM 3, asked for "tree") and
other vegetation (green pixels that are not trees), as shares of each photo's ground.

Measured 2026-10-13 on 61 top-down drone tiles with people's masks (Roboflow "Tree and Vegetation", faiz-ansari,
CC BY 4.0, Hisar, India; some trees were left unlabelled, so the scores are understated): trees, SAM 3 "tree" pixel
IoU 0.62 (found 84%, right 70%; the green rule alone 0.19); other vegetation, green pixels that SAM 3 did not call a
tree, IoU 0.65 (found 78%, right 79%). Buildings, roads and erosion are not measured.

SAM 3 runs on Roboflow's hosted API (hosted_detector): only while HOSTED_FINDER is on, and the photo (shrunk to
SEND_PX on its long side) then leaves this server. Each photo is analysed once (photo_analyses).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np
from PIL import Image, ImageDraw

from src.services import answer_levels, hosted_detector, photo_analyses, photo_inspections
from src.services.photo_inspections import PhotoFacts

METHOD = "sam3-tree+excess-green/v1"  # changes when the method does: results of another method are not reused
TREE_PROMPT = "tree"
SAM3_CONFIDENCE = 0.3  # as measured
GREEN_THRESHOLD = 0.05  # excess green 2G - R - B, on 0..1 channels, as measured
SEND_PX = 1024  # the long side SAM 3 is sent (the measured tiles were 1024 px)
STORE_PREFIX = "towers/surroundings/v1"
MEASURED = {
    "date": "2026-10-13",
    "dataset": "61 top-down drone tiles (Roboflow Tree and Vegetation by faiz-ansari, CC BY 4.0, Hisar, India)",
    "trees_iou": 0.62, "trees_found": 0.84, "trees_right": 0.70,
    "vegetation_iou": 0.65, "vegetation_found": 0.78, "vegetation_right": 0.79,
}


@dataclass(frozen=True)
class Ground:
    """What one photo looking straight down shows: shares of its pixels."""

    trees: float  # SAM 3 "tree"
    other_vegetation: float  # green pixels outside the trees
    trees_found: int  # the tree outlines SAM 3 drew

    def as_dict(self) -> dict[str, Any]:
        return {"method": METHOD, "trees": round(self.trees, 4), "other_vegetation": round(self.other_vegetation, 4),
                "trees_found": self.trees_found}


def enabled() -> bool:
    """SAM 3 can be asked here (HOSTED_FINDER on and a key)."""
    return hosted_detector.enabled() and bool(os.environ.get("ROBOFLOW_API_KEY", "").strip())


def straight_down(photos: Sequence[PhotoFacts]) -> list[PhotoFacts]:
    return [p for p in photos if photo_inspections.view_of(p) == answer_levels.NADIR]


def green(rgb: np.ndarray) -> np.ndarray:
    a = rgb.astype(np.float32) / 255.0
    return (2 * a[..., 1] - a[..., 0] - a[..., 2]) > GREEN_THRESHOLD


def _shrunk(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    scale = SEND_PX / max(height, width)
    if scale >= 1:
        return rgb
    return np.asarray(Image.fromarray(rgb).resize((max(1, round(width * scale)), max(1, round(height * scale))),
                                                  Image.Resampling.BOX))


def ground(rgb: np.ndarray, polygons: Sequence[Sequence[tuple[float, float]]]) -> Ground:
    """The shares of a picture's pixels under the tree outlines, and green outside them."""
    height, width = rgb.shape[:2]
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    for points in polygons:
        if len(points) >= 3:
            draw.polygon(list(points), fill=1)
    trees = np.asarray(mask, bool)
    other = green(rgb) & ~trees
    total = float(height * width)
    return Ground(trees=float(trees.sum()) / total, other_vegetation=float(other.sum()) / total,
                  trees_found=sum(1 for p in polygons if len(p) >= 3))


def analyse(rgb: np.ndarray) -> Ground:
    """Blocking (a Roboflow call): SAM 3's trees and the green rule on the picture, shrunk to SEND_PX first."""
    small = _shrunk(rgb)
    return ground(small, hosted_detector.sam3_polygons(small, TREE_PROMPT, SAM3_CONFIDENCE))


def _result(rgb: np.ndarray) -> dict[str, Any]:
    g = analyse(rgb).as_dict()
    del g["method"]
    return g


JOB = photo_analyses.PhotoAnalysis(name="surroundings", method=METHOD, store_prefix=STORE_PREFIX,
                                   select=straight_down, analyse=_result, enabled=enabled)


async def load(s3: Any, bucket: str, project_id: str, layer_id: str,
               photos: Sequence[PhotoFacts]) -> dict[str, dict[str, Any]]:
    """The stored results of this method for the inspection's photos looking straight down, by photo."""
    return await photo_analyses.load(JOB, s3, bucket, project_id, layer_id, photos)


async def ensure(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, layer_metadata: dict[str, Any],
                 photos: Sequence[PhotoFacts]) -> bool:
    """Analyse, in the background, every photo looking straight down that has no result yet (photo_analyses)."""
    return await photo_analyses.ensure(JOB, redis, s3, bucket, project_id, layer_id, layer_metadata, photos)


def summary(results: dict[str, dict[str, Any]]) -> Optional[dict[str, float]]:
    """The mean shares over the analysed photos; None when none is analysed."""
    if not results:
        return None
    return {"trees": float(np.mean([r["trees"] for r in results.values()])),
            "other_vegetation": float(np.mean([r["other_vegetation"] for r in results.values()]))}
