"""Record one real Ultralytics result for the example finder's tests (test_fixtures/example_finder/).

The unit tests replace the model with a fake that returns THIS recorded result, so the fake has the real shape
(Lessons log: mocks match the real thing). Run inside the app image, with the weights at FINDER_TEST_WEIGHTS:

    docker compose run --rm --no-deps -v <weights>:/w/yoloe-11s-seg.pt:ro -e FINDER_TEST_WEIGHTS=/w/yoloe-11s-seg.pt \
        app python scripts/finder_eval/record_test_result.py

It reads test_fixtures/example_finder/tower_dishes.json (the picture, its dish boxes, the 3 marks) and writes
recorded_yoloe_result.json beside it: the call's arguments and the result's boxes (xyxy, conf, cls) as given by
Ultralytics, with the versions that produced them.
"""

import json
import os
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ["YOLO_OFFLINE"] = "true"
os.environ["YOLO_AUTOINSTALL"] = "False"

import numpy as np  # noqa: E402
import torch  # noqa: E402
import ultralytics  # noqa: E402
from PIL import Image  # noqa: E402
from ultralytics import YOLOE  # noqa: E402
from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor  # noqa: E402

from src.services import example_finder, example_finder_model, stored_files  # noqa: E402

FIXTURES = ROOT / "test_fixtures" / "example_finder"


def main() -> None:
    weights = os.environ["FINDER_TEST_WEIGHTS"]
    assert stored_files.file_sha256(weights) == example_finder_model.MODEL_SHA256, "not the finder's weights"
    spec = json.loads((FIXTURES / "tower_dishes.json").read_text())
    image = Image.open(FIXTURES / spec["picture"]).convert("RGB")
    s = example_finder.settings(spec["target"])
    marks = spec["marks"]
    torch.set_num_threads(example_finder_model.DEFAULT_THREADS)
    began = time.perf_counter()
    result = YOLOE(weights).predict(
        image, refer_image=image,
        visual_prompts={"bboxes": np.asarray(marks, float), "cls": np.zeros(len(marks), int)},
        predictor=YOLOEVPSegPredictor, conf=example_finder.CONF, imgsz=s.finder_input_px, device="cpu",
        verbose=False, max_det=example_finder.MAX_DET)[0]
    seconds = time.perf_counter() - began
    out = {
        "about": "One real Ultralytics result (YOLOE-11s visual prompts) on tower_dishes.jpg with its 3 marks, "
                 "recorded by scripts/finder_eval/record_test_result.py. Tests build their fake model's result "
                 "from these boxes.",
        "recorded": date.today().isoformat(),
        "ultralytics": ultralytics.__version__,
        "torch": torch.__version__,
        "model_sha256": example_finder_model.MODEL_SHA256,
        "call": {"picture": spec["picture"], "prompts": marks, "classes": [0] * len(marks),
                 "imgsz": s.finder_input_px, "conf": example_finder.CONF, "max_det": example_finder.MAX_DET},
        "orig_shape": list(result.orig_shape),
        "seconds": round(seconds, 2),
        "boxes": {"xyxy": result.boxes.xyxy.tolist(), "conf": result.boxes.conf.tolist(),
                  "cls": result.boxes.cls.tolist()},
    }
    (FIXTURES / "recorded_yoloe_result.json").write_text(json.dumps(out, indent=1) + "\n")
    print(f"{len(out['boxes']['conf'])} boxes in {seconds:.1f} s")


if __name__ == "__main__":
    main()
