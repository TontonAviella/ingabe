"""Maize plants (INRAe UAVmulticrop, CC BY): 640 px tiles at full resolution; per field, pool = tiles of 2 photos, test = tiles of the others."""

import os
import json
import random
from pathlib import Path
from PIL import Image

ROOT = Path(os.environ.get("FINDER_EVAL_DIR", Path(__file__).resolve().parent / "work"))
M = ROOT / "raw/maize/UAVmulticrop/maize"
out = ROOT / "raw/maize/tiles"
out.mkdir(exist_ok=True)
T = 640


def tiles(field, img, anns):
    im = Image.open(M / field / img["file_name"]).convert("RGB")
    W, H = im.size
    res = []
    for y0 in range(0, max(H - T // 2, 1), T):
        for x0 in range(0, max(W - T // 2, 1), T):
            x1, y1 = min(x0 + T, W), min(y0 + T, H)
            boxes = []
            for a in anns:
                x, y, w, h = a["bbox"]
                cx, cy = x + w / 2, y + h / 2
                if x0 <= cx < x1 and y0 <= cy < y1:
                    boxes.append([max(x, x0) - x0, max(y, y0) - y0, min(x + w, x1) - x0, min(y + h, y1) - y0])
            f = out / f"{field}_{Path(img['file_name']).stem}_{x0}_{y0}.png"
            if not f.exists():
                im.crop((x0, y0, x1, y1)).save(f)
            res.append({"file": str(f), "boxes": boxes})
    return res


for field in ["3_nerac_2016_1", "2_blois_2019_1", "1_selommes_2019_1"]:
    d = json.load(open(M / f"{field}.json"))
    by = {}
    for a in d["annotations"]:
        by.setdefault(a["image_id"], []).append(a)
    imgs = sorted(d["images"], key=lambda i: i["file_name"])
    random.Random(0).shuffle(imgs)
    pool = [t for i in imgs[:2] for t in tiles(field, i, by.get(i["id"], []))]
    test = [t for i in imgs[2:] for t in tiles(field, i, by.get(i["id"], []))]
    name = field.split("_")[1]
    json.dump({"pool": pool, "test": test}, open(ROOT / "tasks" / f"maize_{name}.json", "w"))
    print(
        name,
        "pool",
        len(pool),
        sum(len(t["boxes"]) for t in pool),
        "test",
        len(test),
        sum(len(t["boxes"]) for t in test),
    )
