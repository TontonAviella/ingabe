"""Same-picture test: mark K boxes on a picture, the model finds the rest there. SAM 3 (sam3.c, EfficientViT-B1 box-tuned)
with the marks, SAM 3 with text only, and YOLOE with the marks. Scored on the unmarked boxes at IoU 0.5 and 0.3."""

import json
import random
import sys
import time
import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import load, evaluate, iou, imgsz, ROOT
from sam3c import detect

TEXT = {
    "tower_mw": "microwave dish",
    "tower_antenna": "panel antenna",
    "tower_rru": "radio unit",
    "maize_nerac": "maize plant",
    "maize_selommes": "maize plant",
    "field_weed": "weed",
    "field_cotton": "cotton plant",
    "kenol_building": "building",
    "trees_forest": "tree",
}
K, N = 3, 20
out = ROOT / "results/sameimage.jsonl"


def keep(preds, marks):
    return [p for p in preds if all(iou(p, m) < 0.3 for m in marks)]


def yoloe(file, marks, size):
    from ultralytics import YOLOE
    from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

    m = YOLOE(str(ROOT / "w/yoloe-11s-seg.pt"))
    t0 = time.perf_counter()
    r = m.predict(
        file,
        refer_image=file,
        visual_prompts={"bboxes": np.array(marks, float), "cls": np.zeros(len(marks), int)},
        predictor=YOLOEVPSegPredictor,
        conf=0.01,
        imgsz=size,
        device="cpu",
        verbose=False,
        max_det=300,
    )[0]
    return [
        [*map(float, b), float(s)] for b, s in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())
    ], time.perf_counter() - t0


if __name__ == "__main__":
    for task in sys.argv[1].split(","):
        test = [i for i in load(task)["test"] if len(i["boxes"]) >= K + 1]
        random.Random(0).shuffle(test)
        test = test[:N]
        rows = {"SAM3 marks": [], "SAM3 text": [], "YOLOE marks": []}
        gts, secs = [], {k: 0.0 for k in rows}
        for im in test:
            g = list(im["boxes"])
            random.Random(im["file"]).shuffle(g)
            marks, rest = g[:K], g[K:]
            gts.append({"boxes": rest})
            W, H = Image.open(im["file"]).size
            for name, kw in [("SAM3 marks", {"boxes": marks}), ("SAM3 text", {"text": TEXT[task]})]:
                try:
                    p, dt = detect(im["file"], model="evit", **kw)
                except Exception as e:
                    print(task, name, "ERR", e, flush=True)
                    p, dt = [], 0.0
                rows[name].append(
                    keep([[b[0] * W / 64, b[1] * H / 64, b[2] * W / 64, b[3] * H / 64, b[4]] for b in p], marks)
                )
                secs[name] += dt
            p, dt = yoloe(im["file"], marks, imgsz(task))
            rows["YOLOE marks"].append(keep(p, marks))
            secs["YOLOE marks"] += dt
        for name, preds in rows.items():
            r = {
                "task": task,
                "method": name,
                "n_img": len(test),
                "sec_per_img": round(secs[name] / len(test), 2),
                "ap50": evaluate(gts, preds, 0.5)["ap50"],
                "ap30": evaluate(gts, preds, 0.3)["ap50"],
                "pr30": [evaluate(gts, preds, 0.3)[k] for k in ("p", "r")],
            }
            open(out, "a").write(json.dumps(r) + "\n")
            print(json.dumps(r), flush=True)
