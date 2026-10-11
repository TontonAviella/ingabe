"""Name the look-alike: same-picture YOLOE with 3 marks of the target AND 3 marks of its look-alike as a second class;
keep only target-class finds. Compared with target marks alone, same pictures, same target marks, same size."""

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
from PIL import Image


def lookalikes(task):
    out = {}
    if task in ("field_weed", "field_weedmix"):
        for t in ("field_cotton",):
            for split in ("pool", "test"):
                for it in load(t)[split]:
                    out.setdefault(it["file"], []).extend(it["boxes"])
        # field_cotton test is a subset; read every tile's cotton boxes straight from the labels instead
        for it in load(task)["test"]:
            f = Path(it["file"])
            w, h = Image.open(f).size
            boxes = []
            for l in open(f.with_suffix(".txt")):
                p = l.split()
                if len(p) >= 5 and p[0] == "1":
                    x, y, bw, bh = map(float, p[1:5])
                    boxes.append([(x - bw / 2) * w, (y - bh / 2) * h, (x + bw / 2) * w, (y + bh / 2) * h])
            out[it["file"]] = boxes
    else:  # radio units: antennas and dishes on the same tower photo
        for t in ("tower_antenna", "tower_mw"):
            for it in load(t)["test"]:
                out.setdefault(it["file"], []).extend(it["boxes"])
    return out


def predict(file, marks, others, size):
    m = YOLOE(str(ROOT / "w/yoloe-11s-seg.pt"))
    boxes = list(marks) + list(others)
    cls = [0] * len(marks) + [1] * len(others)
    t0 = time.perf_counter()
    r = m.predict(
        file,
        refer_image=file,
        visual_prompts={"bboxes": np.array(boxes, float), "cls": np.array(cls, int)},
        predictor=YOLOEVPSegPredictor,
        conf=0.01,
        imgsz=size,
        device="cpu",
        verbose=False,
        max_det=300,
    )[0]
    p = [
        [*map(float, b), float(s)]
        for b, s, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist(), r.boxes.cls.tolist())
        if int(c) == 0
    ]
    return p, time.perf_counter() - t0


for task, size in json.loads(sys.argv[1]).items():
    look = lookalikes(task)
    test = [i for i in load(task)["test"] if len(i["boxes"]) >= K + 1]
    random.Random(0).shuffle(test)
    test = test[:N]
    res = {"alone": ([], 0.0), "with look-alike": ([], 0.0)}
    gts = []
    used = 0
    for im in test:
        g = list(im["boxes"])
        random.Random(im["file"]).shuffle(g)
        marks, rest = g[:K], g[K:]
        gts.append({"boxes": rest})
        o = list(look.get(im["file"], []))
        random.Random("o" + im["file"]).shuffle(o)
        o = o[:K]
        used += bool(o)
        for name, others in (("alone", []), ("with look-alike", o)):
            p, dt = predict(im["file"], marks, others, size)
            res[name][0].append(keep(p, marks))
            res[name] = (res[name][0], res[name][1] + dt)
    for name, (preds, secs) in res.items():
        e = evaluate(gts, preds, 0.5)
        r = {
            "task": task,
            "imgsz": size,
            "method": name,
            "pictures_with_lookalike": used,
            "n_img": len(test),
            "ap50": e["ap50"],
            "p": e["p"],
            "r": e["r"],
            "sec_per_img": round(secs / len(test), 2),
        }
        open(ROOT / "results/contrast.jsonl", "a").write(json.dumps(r) + "\n")
        print(json.dumps(r), flush=True)
