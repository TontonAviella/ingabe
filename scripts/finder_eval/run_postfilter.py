"""Suggestion post-filter, fixed before looking at results: keep boxes whose area is 1/4x..4x the median mark area,
drop a box overlapping a higher-scored kept box at IoU >= 0.5, keep at most 50. Same pictures, marks and sizes as the
same-picture test; scored raw vs filtered (AP50, and P/R at the best-F1 cut), plus how many suggestions a person sees.

-> results/postfilter.jsonl (2026-10-11), copied into src/services/answer_levels_evidence.json; the targets where the
filtered AP50 was at least the raw one have `suggestion_filter` on in answer_levels_targets.json. The app's filter is
example_finder.filter_suggestions (the same rule; its box area max((x1-x0)(y1-y0), 0) equals area() below for every
box with a positive width and height, as all finds and marks have)."""

import json
import random
import statistics
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import ROOT, evaluate, iou, load
from run_sameimage import K, N, keep, yoloe

PLAN = {
    "tower_mw": 1280,
    "tower_antenna": 1280,
    "tower_rru": 2560,
    "maize_nerac": 1280,
    "field_weed": 896,
}


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def postfilter(preds, marks, cap=50):
    m = statistics.median(area(b) for b in marks)
    kept = []
    for p in sorted(preds, key=lambda r: -r[4]):
        if not (m / 4 <= area(p) <= m * 4):
            continue
        if any(iou(p, k) >= 0.5 for k in kept):
            continue
        kept.append(p)
        if len(kept) >= cap:
            break
    return kept


out = ROOT / "results/postfilter.jsonl"
for task, size in PLAN.items():
    test = [i for i in load(task)["test"] if len(i["boxes"]) >= K + 1]
    random.Random(0).shuffle(test)
    test = test[:N]
    gts, raw, filt = [], [], []
    for im in test:
        g = list(im["boxes"])
        random.Random(im["file"]).shuffle(g)
        marks, rest = g[:K], g[K:]
        p, _ = yoloe(im["file"], marks, size)
        p = keep(p, marks)
        gts.append({"boxes": rest})
        raw.append(p)
        filt.append(postfilter(p, marks))
    for name, preds in (("raw", raw), ("filtered", filt)):
        e = evaluate(gts, preds, 0.5)
        r = {
            "task": task,
            "imgsz": size,
            "method": name,
            "ap50": e["ap50"],
            "p": e["p"],
            "r": e["r"],
            "suggestions_per_picture": round(
                sum(len(x) for x in preds) / len(preds), 1
            ),
            "true_per_picture": round(sum(len(g["boxes"]) for g in gts) / len(gts), 1),
        }
        open(out, "a").write(json.dumps(r) + "\n")
        print(json.dumps(r), flush=True)
