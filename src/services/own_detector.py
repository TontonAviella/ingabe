"""Our own trained detectors, as a finder Runner: RF-DETR small models we trained on public data (Kaggle, 2026-10-12),
exported to ONNX and run on this server's CPU. The photos never leave the server and no key is needed. On by default;
OWN_DETECTORS=0 switches them off (the way back: the targets then use the example finder as before, if measured).

Used only for targets whose table entry names one of these models as its detector (answer_levels: `detector`); the
measurement is in answer_levels_targets.json, made with these ONNX files on the held-out photos. The weights come only
from object storage (MODELS[...].key), fetched once and checked by sha256 (stored_files), like the example finder's.
Rules: docs/rules/EXAMPLE_FINDER_RULES.md.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
from PIL import Image

from src.services import stored_files
from src.services.example_finder_model import Row, Runner

PREFIX = "own:"  # a detector's model name in the table: own:<name in MODELS>
THREADS = 4  # measured on the server: 0.86 s a photo at 4 threads, slower at 8 (the app needs the rest)
TOP = 300  # boxes kept before the finder's own score cut, as in RF-DETR's own post-processing
CONFIDENCE = 0.05  # the lowest score kept, as measured (kaggle_rfdetr/onnx_predict.py, threshold 0.05)
_MEAN = np.array([0.485, 0.456, 0.406], np.float32)  # the ImageNet normalisation RF-DETR was trained with
_STD = np.array([0.229, 0.224, 0.225], np.float32)


@dataclass(frozen=True)
class Model:
    key: str  # where the ONNX file is in object storage
    sha256: str
    classes: tuple[str, ...]  # the model's class i is classes[i] (the dataset's classes with boxes, in id order)


MODELS = {
    "own:rfdetr-small-towers-2026-10-12": Model(
        key="models/rfdetr-small-towers-0704243bda5553b2.onnx",
        sha256="0704243bda5553b2c3c2262371442372ab54febed274aaffa31f7bd6776c6728",
        classes=("antenna", "mw", "rru")),
    "own:rfdetr-small-powerlines-2026-10-12": Model(
        key="models/rfdetr-small-powerlines-37900df095d1ec81.onnx",
        sha256="37900df095d1ec814b825592fce11148017ee8459584579574021eaf77461d1f",
        classes=("insulator", "nest", "pylon")),
}

_sessions: dict[str, Any] = {}
_lock = threading.Lock()


def enabled() -> bool:
    """OWN_DETECTORS: 0/false/no switches our detectors off (default on)."""
    return os.environ.get("OWN_DETECTORS", "1").strip().lower() not in {"0", "false", "no"}


def is_detector(model: str) -> bool:
    return model.startswith(PREFIX)


def runner(model: str, detector_class: str, s3: Any, bucket: str) -> Runner:
    """A Runner reading the picture with our model and keeping the boxes of `detector_class` as the target (class 0).
    Blocking (the first call fetches the weights). The marks and the finder size are not used by a trained detector;
    the marks still drop their own boxes later (_find)."""
    spec = MODELS.get(model)
    if spec is None:
        raise ValueError(f"{model!r} is not one of our detectors")
    if detector_class not in spec.classes:
        raise ValueError(f"{model} has no class {detector_class!r}")
    wanted = spec.classes.index(detector_class)
    session = _session(model, spec, s3, bucket)

    def ask(rgb: np.ndarray, prompts: Sequence[Sequence[float]], classes: Sequence[int], imgsz: int, conf: float,
            max_det: int) -> list[Row]:
        boxes, logits = session.run(None, {session.get_inputs()[0].name: _input(rgb, session.get_inputs()[0].shape[2])})
        rows = [r for r in _rows(boxes[0], logits[0], rgb.shape[1], rgb.shape[0]) if r[5] == wanted and r[4] >= max(conf, CONFIDENCE)]
        return [(x0, y0, x1, y1, score, 0) for x0, y0, x1, y1, score, _ in rows][:max_det]

    return ask


def _session(model: str, spec: Model, s3: Any, bucket: str) -> Any:
    with _lock:
        if model not in _sessions:
            import onnxruntime  # only the servers that run a detector load it

            path = stored_files.ensure_local_copy(s3, bucket, spec.key, spec.sha256,
                                                  f"/tmp/ingabe_cache/models/{spec.key.rsplit('/', 1)[-1]}",
                                                  f"detector model {model}")
            options = onnxruntime.SessionOptions()
            options.intra_op_num_threads = THREADS
            _sessions[model] = onnxruntime.InferenceSession(path, options, providers=["CPUExecutionProvider"])
        return _sessions[model]


def _input(rgb: np.ndarray, size: int) -> np.ndarray:
    """The picture as the model reads it: squashed to size x size, scaled to 0..1, normalised, channels first."""
    x = np.asarray(Image.fromarray(rgb).convert("RGB").resize((size, size), Image.BILINEAR), np.float32) / 255.0
    return ((x - _MEAN) / _STD).transpose(2, 0, 1)[None]


def _rows(boxes: np.ndarray, logits: np.ndarray, width: int, height: int) -> list[Row]:
    """RF-DETR's post-processing: every (query, class) pair scored by the sigmoid of its logit, the TOP best kept, best
    first; boxes are centre/size in 0..1 of the picture. The last field is the model's class index."""
    scores = 1 / (1 + np.exp(-logits.astype(np.float64)))
    n_classes = scores.shape[1]
    flat = scores.reshape(-1)
    rows: list[Row] = []
    for i in np.argsort(-flat, kind="stable")[:TOP]:
        query, cls = divmod(int(i), n_classes)
        cx, cy, w, h = (float(v) for v in boxes[query])
        rows.append(((cx - w / 2) * width, (cy - h / 2) * height, (cx + w / 2) * width, (cy + h / 2) * height,
                     float(flat[i]), cls))
    return rows
