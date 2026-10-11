"""NEON tree crowns (10 cm): SJER oak savanna = bush trees, TEAK conifer forest = forest trees. Pool and test tiles differ."""

import os
import io
import json
import random
from pathlib import Path
import pyarrow.parquet as pq
from PIL import Image

ROOT = Path(os.environ.get("FINDER_EVAL_DIR", Path(__file__).resolve().parent / "work"))
out = ROOT / "raw/trees/img"
out.mkdir(exist_ok=True)
rows = pq.read_table(ROOT / "raw/trees/test.parquet").to_pylist()


def site(n):
    p = n.split("_")
    return p[1] if p[0] == "2018" else p[0]


for s, name, npool in [("SJER", "savanna", 20), ("TEAK", "forest", 15)]:
    items = []
    for r in rows:
        if site(r["tile_name"]) != s:
            continue
        f = out / r["tile_name"].replace(".tif", ".png")
        if not f.exists():
            Image.open(io.BytesIO(r["image"]["bytes"])).convert("RGB").save(f)
        items.append({"file": str(f), "boxes": [[x, y, x + w, y + h] for x, y, w, h in r["annotations"]["bbox"]]})
    random.Random(0).shuffle(items)
    task = {"pool": items[:npool], "test": items[npool:]}
    json.dump(task, open(ROOT / "tasks" / f"trees_{name}.json", "w"))
    print(name, {k: (len(v), sum(len(i["boxes"]) for i in v)) for k, v in task.items()})
