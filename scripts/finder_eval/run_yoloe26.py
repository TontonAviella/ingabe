"""YOLOE-11 vs YOLOE-26, same-picture test (3 marks on the picture, find the rest), same pictures, marks and sizes."""

import json
import random
import sys
import time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import load, evaluate, ROOT
from run_sameimage import keep, K, N
from ultralytics import YOLOE
from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor


def predict(weights, file, marks, size):
    m = YOLOE(str(ROOT / "w" / weights))
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
    plan = json.loads(sys.argv[1])
    models = sys.argv[2].split(",")
    for task, size in plan.items():
        test = [i for i in load(task)["test"] if len(i["boxes"]) >= K + 1]
        random.Random(0).shuffle(test)
        test = test[:N]
        for w in models:
            gts, preds, secs = [], [], 0.0
            for im in test:
                g = list(im["boxes"])
                random.Random(im["file"]).shuffle(g)
                marks, rest = g[:K], g[K:]
                p, dt = predict(w, im["file"], marks, size)
                preds.append(keep(p, marks))
                gts.append({"boxes": rest})
                secs += dt
            e = evaluate(gts, preds, 0.5)
            r = {
                "task": task,
                "imgsz": size,
                "model": w,
                "ap50": e["ap50"],
                "p": e["p"],
                "r": e["r"],
                "sec_per_img": round(secs / len(test), 2),
            }
            open(ROOT / "results/yoloe26.jsonl", "a").write(json.dumps(r) + "\n")
            print(json.dumps(r), flush=True)
