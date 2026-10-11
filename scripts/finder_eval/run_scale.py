"""Scale from context: same-picture YOLOE (3 marks, find the rest) at the normal size vs enlarged so objects are ~50-60 px."""

import json
import random
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import load, evaluate, ROOT
from run_sameimage import yoloe, keep, K, N

PLAN = {
    "maize_selommes": [640, 1280],
    "tower_rru": [1280, 2560, 3200],
    "field_weed": [640, 896],
    "tower_mw": [1280, 2304],
    "maize_nerac": [640, 1280],
}
out = ROOT / "results/scale.jsonl"
if len(sys.argv) > 1:
    PLAN = json.loads(sys.argv[1])
for task, sizes in PLAN.items():
    test = [i for i in load(task)["test"] if len(i["boxes"]) >= K + 1]
    random.Random(0).shuffle(test)
    test = test[:N]
    for size in sizes:
        gts, preds, secs = [], [], 0.0
        for im in test:
            g = list(im["boxes"])
            random.Random(im["file"]).shuffle(g)
            marks, rest = g[:K], g[K:]
            p, dt = yoloe(im["file"], marks, size)
            preds.append(keep(p, marks))
            gts.append({"boxes": rest})
            secs += dt
        r = {
            "task": task,
            "imgsz": size,
            "ap50": evaluate(gts, preds, 0.5)["ap50"],
            "p_r": [evaluate(gts, preds, 0.5)[k] for k in ("p", "r")],
            "sec_per_img": round(secs / len(test), 2),
        }
        open(out, "a").write(json.dumps(r) + "\n")
        print(json.dumps(r), flush=True)
