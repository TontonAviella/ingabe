"""Where the conductors run on a power line inspection's photos: SAM 3, asked for "power line", outlines them on
each photo.

Measured 2026-10-13 on 40 photos of TTPLA (Abdelfattah et al., Apache 2.0; 3840 x 2160 px drone photos of power
lines, sites none of our models were trained on, shrunk to 1536 px wide, people's "void" areas left out): pixel IoU
0.54 against people's cable outlines; with a 4 px tolerance (a conductor is a few pixels wide), 97% of the cable
people outlined was within SAM 3's outline and 83% of SAM 3's outline was on a cable; no photo came back empty.
Sag, clearance, broken strands and what grows near the conductors are not measured.

SAM 3 runs on Roboflow's hosted API (hosted_detector): only while HOSTED_FINDER is on, and the photo (shrunk to
SEND_PX on its long side) then leaves this server. Each photo is analysed once (photo_analyses)."""

from __future__ import annotations

import os
from typing import Any, Sequence

import numpy as np
from PIL import Image

from src.services import hosted_detector, photo_analyses, photo_inspections
from src.services.photo_inspections import PhotoFacts

METHOD = "sam3-power-line/v1"  # changes when the method does: results of another method are not reused
PROMPT = "power line"
SAM3_CONFIDENCE = 0.3  # as measured
SEND_PX = 1536  # the long side SAM 3 is sent, as measured
STORE_PREFIX = "towers/conductors/v1"
MEASURED = {
    "date": "2026-10-13",
    "dataset": "40 drone photos of power lines at sites our models never saw (TTPLA, Apache 2.0)",
    "n_images": 40, "iou": 0.54, "found": 0.97, "right": 0.83, "tolerance_px": 4,
}


def enabled() -> bool:
    """SAM 3 can be asked here (HOSTED_FINDER on and a key)."""
    return hosted_detector.enabled() and bool(os.environ.get("ROBOFLOW_API_KEY", "").strip())


def readable(photos: Sequence[PhotoFacts]) -> list[PhotoFacts]:
    """The photos whose view is known (the photos the scan reads)."""
    return [p for p in photos if photo_inspections.view_of(p) is not None]


def _shrunk(rgb: np.ndarray) -> tuple[np.ndarray, float]:
    height, width = rgb.shape[:2]
    scale = SEND_PX / max(height, width)
    if scale >= 1:
        return rgb, 1.0
    small = Image.fromarray(rgb).resize((max(1, round(width * scale)), max(1, round(height * scale))),
                                        Image.Resampling.BOX)
    return np.asarray(small), scale


def outlines(polygons: Sequence[Sequence[tuple[float, float]]], scale: float) -> list[list[list[int]]]:
    """The conductor outlines in the photo's own pixels (SAM 3 saw it shrunk by `scale`)."""
    return [[[round(x / scale), round(y / scale)] for x, y in points] for points in polygons if len(points) >= 3]


def analyse(rgb: np.ndarray) -> dict[str, Any]:
    """Blocking (a Roboflow call): the conductor outlines SAM 3 draws on the photo, in the photo's own pixels."""
    small, scale = _shrunk(rgb)
    found = outlines(hosted_detector.sam3_polygons(small, PROMPT, SAM3_CONFIDENCE), scale)
    return {"outlines": found, "width": int(rgb.shape[1]), "height": int(rgb.shape[0])}


JOB = photo_analyses.PhotoAnalysis(name="conductors", method=METHOD, store_prefix=STORE_PREFIX, select=readable,
                                   analyse=analyse, enabled=enabled)


async def load(s3: Any, bucket: str, project_id: str, layer_id: str,
               photos: Sequence[PhotoFacts]) -> dict[str, dict[str, Any]]:
    """The stored results of this method for the inspection's photos, by photo."""
    return await photo_analyses.load(JOB, s3, bucket, project_id, layer_id, photos)


async def ensure(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, layer_metadata: dict[str, Any],
                 photos: Sequence[PhotoFacts]) -> bool:
    """Outline the conductors, in the background, on every photo that has no result yet (photo_analyses)."""
    return await photo_analyses.ensure(JOB, redis, s3, bucket, project_id, layer_id, layer_metadata, photos)
