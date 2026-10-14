"""The FastSAM model this server runs (FastSAM-s.pt, Ultralytics), shared by everything that outlines objects with
it: orthophoto object outlines (raster_object_candidates) and the outlines of parts on inspection photos
(part_outlines). One model in memory, one run at a time."""

from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

# Ultralytics model objects are not safe to call from several threads, and the model is shared via load()'s cache;
# one 4 MP run is ~40 s of CPU. Inference is serialized; a caller that cannot start within its own wait reports busy
# instead of queueing on.
INFERENCE_LOCK = threading.Lock()


def weights_status() -> dict[str, Any]:
    """Where the weights are (MUNDI_FASTSAM_WEIGHTS, the project root, the working directory or /app), or why none
    were found."""
    candidates = []
    env_path = os.environ.get("MUNDI_FASTSAM_WEIGHTS")
    if env_path:
        candidates.append(Path(env_path))
    project_root = Path(__file__).resolve().parents[2]
    candidates.extend([project_root / "FastSAM-s.pt", Path.cwd() / "FastSAM-s.pt", Path("/app/FastSAM-s.pt")])
    for candidate in candidates:
        if candidate.exists():
            return {"available": True, "path": str(candidate), "size_bytes": candidate.stat().st_size}
    return {
        "available": False,
        "reason": "FastSAM-s.pt not found; set MUNDI_FASTSAM_WEIGHTS or place it at the project root.",
        "checked_paths": [str(path) for path in candidates],
    }


@lru_cache(maxsize=2)
def load(weights_path: str) -> Any:
    from ultralytics import FastSAM  # lazy: heavy optional dependency, loaded only when something is outlined

    return FastSAM(weights_path)


def device() -> str:
    return os.environ.get("MUNDI_FASTSAM_DEVICE", "cpu")
