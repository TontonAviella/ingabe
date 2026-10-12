"""Trained detectors hosted by Roboflow, as a finder Runner: the photo goes to Roboflow's serverless API and the
boxes of one class come back. Used only for targets whose table entry names a detector (answer_levels: `detector`)
and only while HOSTED_FINDER is on: with it on, the photos of those targets leave this server for Roboflow.

The models were trained on Roboflow from public data (2026-10-12) and measured on held-out photos; the measurement
is in answer_levels_targets.json. Key: ROBOFLOW_API_KEY (never logged). Rules: docs/rules/EXAMPLE_FINDER_RULES.md.
"""

from __future__ import annotations

import base64
import io
import os
from typing import Sequence

import numpy as np
import requests
from PIL import Image

from src.services.example_finder_model import Row, Runner

_SWITCH = "HOSTED_FINDER"
PREFIX = "roboflow:"  # a detector's model name in the table: roboflow:<workspace>/<model id>
URL = "https://serverless.roboflow.com/"
SAM3_MODEL = "sam3/sam3_final"  # SAM 3 with a typed prompt (the detector's class is the prompt), as a Roboflow workflow
TIMEOUT_S = 120
CONFIDENCE = 0.05  # the lowest score asked for, as measured (scripts: eval_coco_hosted.py, confidence 5)
OVERLAP = 0.3


class DetectorUnavailable(RuntimeError):
    """The detector cannot be asked: switched off or no key (the message says which)."""


def enabled() -> bool:
    """HOSTED_FINDER: 1/true/yes lets the finder send photos to Roboflow for targets with a detector (default off)."""
    return os.environ.get(_SWITCH, "0").strip().lower() in {"1", "true", "yes"}


def is_detector(model: str) -> bool:
    return model.startswith(PREFIX)


def runner(model: str, detector_class: str) -> Runner:
    """A Runner asking the hosted model and keeping the boxes of `detector_class` as the target (class 0). The marks
    and the finder size are not used by a trained detector; the marks still drop their own boxes later (_find)."""
    if not is_detector(model):
        raise ValueError(f"{model!r} is not a hosted detector")
    if not enabled():
        raise DetectorUnavailable("the trained model is switched off on this server (HOSTED_FINDER)")
    key = os.environ.get("ROBOFLOW_API_KEY", "").strip()
    if not key:
        raise DetectorUnavailable("the trained model has no key on this server (ROBOFLOW_API_KEY)")
    model_id = model[len(PREFIX):]
    if model_id == SAM3_MODEL:
        return _sam3(key, detector_class)

    def ask(rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int], imgsz: int, conf: float,
            max_det: int) -> list[Row]:
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "JPEG", quality=92)
        response = requests.post(URL + model_id, params={"api_key": key, "confidence": int(CONFIDENCE * 100),
                                                         "overlap": int(OVERLAP * 100)},
                                 data=base64.b64encode(buf.getvalue()),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=TIMEOUT_S)
        if response.status_code != 200:  # the key is in the URL: never put the response or URL in the message
            raise DetectorUnavailable(f"the trained model answered HTTP {response.status_code}")
        rows: list[Row] = []
        for p in response.json().get("predictions", []):
            if p.get("class") != detector_class or p["confidence"] < conf:
                continue
            x, y, w, h = p["x"], p["y"], p["width"], p["height"]
            rows.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2, float(p["confidence"]), 0))
        return sorted(rows, key=lambda r: -r[4])[:max_det]

    return ask


def _sam3(key: str, prompt: str) -> Runner:
    """SAM 3 asked for `prompt` (e.g. "seedling") through a one-step Roboflow workflow; every box it returns is the
    target (one prompt), kept from CONFIDENCE up, as measured (scripts: sam3_run.py, confidence 0.05)."""
    spec = {"version": "1.0", "inputs": [{"type": "WorkflowImage", "name": "image"}],
            "steps": [{"type": "roboflow_core/sam3@v3", "name": "sam", "images": "$inputs.image",
                       "class_names": [prompt], "confidence": CONFIDENCE, "output_format": "polygons"}],
            "outputs": [{"type": "JsonField", "name": "out", "selector": "$steps.sam.predictions"}]}

    def ask(rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int], imgsz: int, conf: float,
            max_det: int) -> list[Row]:
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, "JPEG", quality=92)
        response = requests.post(URL + "workflows/run", timeout=TIMEOUT_S,
                                 json={"api_key": key, "specification": spec,
                                       "inputs": {"image": {"type": "base64",
                                                            "value": base64.b64encode(buf.getvalue()).decode()}}})
        if response.status_code != 200:  # the key is in the body: never put the response in the message
            raise DetectorUnavailable(f"SAM 3 answered HTTP {response.status_code}")
        rows: list[Row] = []
        for p in response.json()["outputs"][0]["out"].get("predictions", []):
            if p["confidence"] < conf:
                continue
            x, y, w, h = p["x"], p["y"], p["width"], p["height"]
            rows.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2, float(p["confidence"]), 0))
        return sorted(rows, key=lambda r: -r[4])[:max_det]

    return ask
