"""What a person SEES on the photo-check screen for maize, scored against INRAe people's boxes.

Stage 1 (`run`): the app's finder protocol per tile (3 people's boxes as marks on the same tile, YOLOE-11s visual
prompts, conf 0.01, imgsz 1280, finds overlapping a mark at IoU >= 0.3 dropped) -> raw finds cached in
results/raw.jsonl. Stage 2 (`score`): screen rules applied offline, all fixed BEFORE looking at results:
  current  = the app today: area 1/4..4x median mark, IoU >= 0.5 duplicates dropped, at most 50, no score cut
  nested   = current + a find whose overlap covers >= 60% of the smaller of it and a better kept find is dropped
  cut_abs  = nested + score >= c (c chosen on CALIBRATION tiles by best F1 of the shown set)
  cut_rel  = nested + score >= r * best score on that tile (r chosen on CALIBRATION tiles)
Calibration tiles come from the 2 pool photos of each field, test tiles from the other photos (never mixed).
Reported on TEST tiles: precision and recall of the shown boxes at IoU 0.5, shown vs true per tile, by field.

-> results/raw.jsonl, results/shown.json (2026-11-01, run inside the app image, mundi-public:local, amd64 on the M1
Mac), copied into src/services/answer_levels_evidence.json; the chosen rule is maize_seedling's `shown` entry in
answer_levels_targets.json, applied by example_finder.shown_finds (overlap of the smaller box as boxes.overlap_of_smaller)."""

import json
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(os.environ.get("FINDER_EVAL_DIR", Path(__file__).resolve().parent / "work"))
FIELDS = ["nerac", "blois", "selommes"]
K, N_CAL, N_TEST, SIZE = 3, 15, 25, 1280


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def ios(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    small = min(area(a), area(b))
    return ix * iy / small if small > 0 else 0.0


def area(b):
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def pick(split, n):
    out = []
    for f in FIELDS:
        tiles = [t for t in json.load(open(ROOT / f"tasks/maize_{f}.json"))[split] if len(t["boxes"]) >= K + 1]
        random.Random(f"{f}-{split}").shuffle(tiles)
        out += [dict(t, field=f, split=split) for t in tiles[:n]]
    return out


def run():
    import numpy as np
    from ultralytics import YOLOE
    from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

    path = ROOT / "results/raw.jsonl"
    done = {json.loads(line)["file"] for line in open(path)} if path.exists() else set()
    for t in pick("pool", N_CAL) + pick("test", N_TEST):
        if t["file"] in done:
            continue
        g = list(t["boxes"])
        random.Random(t["file"]).shuffle(g)
        marks, rest = g[:K], g[K:]
        m = YOLOE(str(ROOT / "w/yoloe-11s-seg.pt"))
        t0 = time.perf_counter()
        r = m.predict(t["file"], refer_image=t["file"], predictor=YOLOEVPSegPredictor,
                      visual_prompts={"bboxes": np.array(marks, float), "cls": np.zeros(len(marks), int)},
                      conf=0.01, imgsz=SIZE, device="cpu", verbose=False, max_det=300)[0]
        finds = [[*map(float, b), float(s)] for b, s in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]
        finds = [p for p in finds if all(iou(p, mk) < 0.3 for mk in marks)]
        row = {"file": t["file"], "field": t["field"], "split": t["split"], "marks": marks, "truth": rest,
               "finds": finds, "sec": round(time.perf_counter() - t0, 2)}
        open(path, "a").write(json.dumps(row) + "\n")
        print(t["field"], t["split"], len(rest), len(finds), row["sec"], flush=True)


def current(finds, marks):
    med = statistics.median(area(m) for m in marks)
    kept = []
    for p in sorted(finds, key=lambda r: -r[4]):
        if med / 4 <= area(p) <= med * 4 and not any(iou(p, k) >= 0.5 for k in kept):
            kept.append(p)
            if len(kept) >= 50:
                break
    return kept


def nested(finds, marks):
    kept = []
    for p in current(finds, marks):
        if not any(ios(p, k) >= 0.6 for k in kept):
            kept.append(p)
    return kept


def shown_score(rows, shown):
    tp = fp = n_true = 0
    for row, S in zip(rows, shown):
        n_true += len(row["truth"])
        used = set()
        for p in sorted(S, key=lambda r: -r[4]):
            best, bj = 0.0, -1
            for j, g in enumerate(row["truth"]):
                if j not in used and iou(p, g) > best:
                    best, bj = iou(p, g), j
            if best >= 0.5:
                used.add(bj)
                tp += 1
            else:
                fp += 1
    p = tp / max(tp + fp, 1)
    r = tp / max(n_true, 1)
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(2 * p * r / max(p + r, 1e-9), 3),
            "shown_per_tile": round((tp + fp) / len(rows), 1), "true_per_tile": round(n_true / len(rows), 1),
            "tiles": len(rows)}


def rules(cut_abs, cut_rel):
    return {
        "current": lambda f, m: current(f, m),
        "nested": lambda f, m: nested(f, m),
        "cut_abs": lambda f, m: [p for p in nested(f, m) if p[4] >= cut_abs],
        "cut_rel": lambda f, m: (lambda S: [p for p in S if S and p[4] >= cut_rel * max(q[4] for q in S)])(nested(f, m)),
    }


def score():
    rows = [json.loads(line) for line in open(ROOT / "results/raw.jsonl")]
    cal = [r for r in rows if r["split"] == "pool"]
    test = [r for r in rows if r["split"] == "test"]
    grid = [round(0.02 * i, 2) for i in range(1, 46)]
    best_abs = max(grid, key=lambda c: shown_score(cal, [rules(c, 0)["cut_abs"](r["finds"], r["marks"]) for r in cal])["f1"])
    best_rel = max(grid, key=lambda c: shown_score(cal, [rules(0, c)["cut_rel"](r["finds"], r["marks"]) for r in cal])["f1"])
    print(json.dumps({"chosen_on_calibration": {"cut_abs": best_abs, "cut_rel": best_rel},
                      "calibration_tiles": len(cal), "test_tiles": len(test)}))
    out = []
    for name, rule in rules(best_abs, best_rel).items():
        for field in ["all"] + FIELDS:
            sub = [r for r in test if field in ("all", r["field"])]
            res = dict(rule=name, field=field, **shown_score(sub, [rule(r["finds"], r["marks"]) for r in sub]))
            out.append(res)
            print(json.dumps(res))
    json.dump(out, open(ROOT / "results/shown.json", "w"), indent=1)


if __name__ == "__main__":
    {"run": run, "score": score}[sys.argv[1]]()
