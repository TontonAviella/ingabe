"""Look-alike marks on towers, more evidence: every tower photo with enough targets, 3 mark draws each.
Target marks alone vs target marks + look-alike marks (other tower parts on the same photo, as a second class)."""

import json
import random
import sys
import time
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from common import load, evaluate, ROOT
from run_sameimage import keep
from ultralytics import YOLOE
from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

LOOK = {
    "tower_rru": ["tower_antenna", "tower_mw"],
    "tower_antenna": ["tower_rru", "tower_mw"],
    "tower_mw": ["tower_antenna"],
}


def predict(file, marks, others, size):
    m = YOLOE(str(ROOT / "w/yoloe-11s-seg.pt"))
    r = m.predict(
        file,
        refer_image=file,
        visual_prompts={
            "bboxes": np.array(list(marks) + list(others), float),
            "cls": np.array([0] * len(marks) + [1] * len(others), int),
        },
        predictor=YOLOEVPSegPredictor,
        conf=0.01,
        imgsz=size,
        device="cpu",
        verbose=False,
        max_det=300,
    )[0]
    return [
        [*map(float, b), float(s)]
        for b, s, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist())
        if int(c) == 0
    ]


for spec in json.loads(sys.argv[1]):  # [{"task":..., "size":..., "k":..., "seeds":[...]}]
    task, size, k = spec["task"], spec["size"], spec["k"]
    look = {}
    for t in LOOK[task]:
        for it in load(t)["test"]:
            look.setdefault(it["file"], []).extend(it["boxes"])
    test = [i for i in load(task)["test"] if len(i["boxes"]) >= k + 1]
    for seed in spec["seeds"]:
        res = {"alone": [], "with look-alike": []}
        gts = []
        t0 = time.time()
        for im in test:
            g = list(im["boxes"])
            random.Random(f"{seed}{im['file']}").shuffle(g)
            marks, rest = g[:k], g[k:]
            gts.append({"boxes": rest})
            o = list(look.get(im["file"], []))
            random.Random(f"o{seed}{im['file']}").shuffle(o)
            o = o[:k]
            res["alone"].append(keep(predict(im["file"], marks, [], size), marks))
            res["with look-alike"].append(keep(predict(im["file"], marks, o, size), marks))
        for name, preds in res.items():
            e = evaluate(gts, preds, 0.5)
            r = {
                "task": task,
                "k": k,
                "seed": seed,
                "imgsz": size,
                "method": name,
                "n_img": len(test),
                "ap50": e["ap50"],
                "p": e["p"],
                "r": e["r"],
            }
            open(ROOT / "results/lookalike_towers.jsonl", "a").write(json.dumps(r) + "\n")
            print(json.dumps(r), flush=True)
        print("seconds", round(time.time() - t0), flush=True)
