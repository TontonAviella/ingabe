"""Build contest tasks: {pool: [...], test: [...]} with xyxy boxes, images as JPG."""

import os
import json
import random
import sys
from pathlib import Path
import numpy as np
import cv2
from scipy import ndimage

S = Path(sys.argv[1])
OUT = Path(sys.argv[2])
OUT.mkdir(exist_ok=True)
TOW = Path(os.environ.get("TOWERS_COCO_DIR", "/Users/macbook/Ingabe/data/towers/datasets/celltower_1333"))


def towers():
    for cls, name in [(1, "antenna"), (2, "mw"), (3, "rru")]:
        task = {}
        for split, key in [("train", "pool"), ("test", "test")]:
            d = json.load(open(TOW / split / "_annotations.coco.json"))
            by = {}
            for a in d["annotations"]:
                if a["category_id"] == cls:
                    x, y, w, h = a["bbox"]
                    by.setdefault(a["image_id"], []).append([x, y, x + w, y + h])
            task[key] = [
                {"file": str(TOW / split / im["file_name"]), "boxes": by.get(im["id"], [])} for im in d["images"]
            ]
        json.dump(task, open(OUT / f"tower_{name}.json", "w"))
        print(name, {k: (len(v), sum(len(i["boxes"]) for i in v)) for k, v in task.items()})


def kenol():
    img_dir = OUT / "kenol_img"
    img_dir.mkdir(exist_ok=True)

    def load(name):
        lab = cv2.imread(str(S / "kenol/Labels" / name), cv2.IMREAD_UNCHANGED)
        lab = lab if lab.ndim == 2 else lab[..., 0]
        m = (lab == 1) | (lab == 3)
        cc, n = ndimage.label(m)
        boxes = []
        for sl in ndimage.find_objects(cc):
            y, x = sl
            if (y.stop - y.start) * (x.stop - x.start) >= 200:
                boxes.append([x.start, y.start, x.stop, y.stop])
        return boxes

    def rgb(name):
        out = img_dir / name.replace(".tif", ".jpg")
        if not out.exists():
            im = cv2.imread(str(S / "kenol/Images" / name), cv2.IMREAD_UNCHANGED)
            cv2.imwrite(str(out), im[..., :3], [cv2.IMWRITE_JPEG_QUALITY, 95])
        return str(out)

    names = sorted(p.name for p in (S / "kenol/Labels").glob("*.tif"))
    rng = random.Random(0)
    k1 = [n for n in names if n.startswith("kenol1")]
    k2 = [n for n in names if n.startswith("kenol2")]
    rng.shuffle(k1)
    rng.shuffle(k2)
    pool = []
    for n in k1:
        b = load(n)
        if b:
            pool.append({"file": rgb(n), "boxes": b})
        if len(pool) >= 120:
            break
    test, empty = [], []
    for n in k2:
        b = load(n)
        if b and len(test) < 60:
            test.append({"file": rgb(n), "boxes": b})
        elif not b and len(empty) < 20:
            empty.append({"file": rgb(n), "boxes": []})
        if len(test) >= 60 and len(empty) >= 20:
            break
    task = {"pool": pool, "test": test + empty}
    json.dump(task, open(OUT / "kenol_building.json", "w"))
    print("kenol", {k: (len(v), sum(len(i["boxes"]) for i in v)) for k, v in task.items()})


towers()
kenol()
