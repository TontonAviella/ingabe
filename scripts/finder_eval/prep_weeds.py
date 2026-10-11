"""Weed / cotton drone tiles (YOLO labels: 0 weed, 1 cotton). Example and test tiles come from different source photos."""

import os
import glob
import json
import random
from pathlib import Path
from PIL import Image

ROOT = Path(os.environ.get("FINDER_EVAL_DIR", Path(__file__).resolve().parent / "work"))
src = ROOT / "raw/weeds/image_dataset"
items = {}
for f in sorted(src.glob("*.jpg")):
    lab = f.with_suffix(".txt")
    w, h = Image.open(f).size
    boxes = {0: [], 1: []}
    if lab.exists():
        for l in open(lab):
            p = l.split()
            if len(p) >= 5:
                c, x, y, bw, bh = int(p[0]), *map(float, p[1:5])
                boxes[c].append([(x - bw / 2) * w, (y - bh / 2) * h, (x + bw / 2) * w, (y + bh / 2) * h])
    items.setdefault(f.stem.rsplit("_", 1)[0], []).append((str(f), boxes))
photos = sorted(items)
rng = random.Random(0)
rng.shuffle(photos)
pool_ph, test_ph = photos[:128], photos[128:]
for cls, name in [(0, "weed"), (1, "cotton")]:
    pool = [{"file": f, "boxes": b[cls]} for p in pool_ph for f, b in items[p]]
    tiles = [{"file": f, "boxes": b[cls]} for p in test_ph for f, b in items[p]]
    rng2 = random.Random(1)
    rng2.shuffle(tiles)
    pos = [t for t in tiles if t["boxes"]][:80]
    neg = [t for t in tiles if not t["boxes"]][:20]
    task = {"pool": pool, "test": pos + neg}
    json.dump(task, open(ROOT / "tasks" / f"field_{name}.json", "w"))
    print(
        name,
        "pool",
        len(pool),
        sum(len(i["boxes"]) for i in pool),
        "test",
        len(task["test"]),
        sum(len(i["boxes"]) for i in task["test"]),
    )
