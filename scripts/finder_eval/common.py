import os
import json
import random
from pathlib import Path
import numpy as np

ROOT = Path(os.environ.get("FINDER_EVAL_DIR", Path(__file__).resolve().parent / "work"))
TASKS = ["tower_antenna", "tower_mw", "tower_rru", "kenol_building"]
SHOTS = [1, 5, 10]
SEEDS = [0, 1, 2]
IMGSZ = {"tower": 1280, "kenol": 640, "field": 640, "fieldmix": 640, "trees": 640, "maize": 640}


def load(task):
    return json.load(open(ROOT / "tasks" / f"{task}.json"))


def imgsz(task):
    return IMGSZ[task.split("_")[0]]


def examples(task, k, seed):
    """k marked boxes: random pool images in random order, boxes taken until k (what a person marks)."""
    pool = [p for p in load(task)["pool"] if p["boxes"]]
    rng = random.Random(f"{task}-{seed}")
    rng.shuffle(pool)
    out, n = [], 0
    for p in pool:
        b = list(p["boxes"])
        rng.shuffle(b)
        take = b[: k - n]
        out.append({"file": p["file"], "boxes": take})
        n += len(take)
        if n >= k:
            break
    return out


def full_images(task, min_boxes, seed):
    """Whole pool images (every box labelled) until at least min_boxes: what a detector must train on."""
    pool = [p for p in load(task)["pool"] if p["boxes"]]
    rng = random.Random(f"{task}-full-{seed}")
    rng.shuffle(pool)
    out, n = [], 0
    for p in pool:
        out.append(p)
        n += len(p["boxes"])
        if n >= min_boxes:
            break
    return out


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i
    return i / u if u > 0 else 0.0


def evaluate(test, preds, thr=0.5):
    """preds: list (per test image) of [x0,y0,x1,y1,score]. Returns AP50 and P/R/F1 at the best-F1 score cut."""
    rows, n_gt = [], 0
    for im, P in zip(test, preds):
        gt = im["boxes"]
        n_gt += len(gt)
        used = set()
        for p in sorted(P, key=lambda r: -r[4]):
            best, bj = 0, -1
            for j, g in enumerate(gt):
                if j in used:
                    continue
                v = iou(p, g)
                if v > best:
                    best, bj = v, j
            if best >= thr:
                used.add(bj)
                rows.append((p[4], 1))
            else:
                rows.append((p[4], 0))
    if not rows:
        return {"ap50": 0.0, "p": 0.0, "r": 0.0, "f1": 0.0, "n_gt": n_gt}
    rows.sort(key=lambda r: -r[0])
    tp = np.cumsum([r[1] for r in rows])
    fp = np.cumsum([1 - r[1] for r in rows])
    rec = tp / max(n_gt, 1)
    prec = tp / (tp + fp)
    # all-point interpolated AP
    mrec = np.concatenate([[0], rec, [1]])
    mpre = np.concatenate([[1], prec, [0]])
    for i in range(len(mpre) - 2, -1, -1):
        mpre[i] = max(mpre[i], mpre[i + 1])
    idx = np.where(mrec[1:] != mrec[:-1])[0]
    ap = float(np.sum((mrec[idx + 1] - mrec[idx]) * mpre[idx + 1]))
    f1 = 2 * prec * rec / np.maximum(prec + rec, 1e-9)
    i = int(np.argmax(f1))
    return {
        "ap50": round(ap, 3),
        "p": round(float(prec[i]), 3),
        "r": round(float(rec[i]), 3),
        "f1": round(float(f1[i]), 3),
        "cut": round(float(rows[i][0]), 3),
        "n_gt": n_gt,
    }


def done(method, task, k, seed):
    f = ROOT / "results" / "scores.jsonl"
    if not f.exists():
        return False
    return any((r["method"], r["task"], r["k"], r["seed"]) == (method, task, k, seed) for r in map(json.loads, open(f)))


def save(method, task, k, seed, preds, sec_per_img, extra=None):
    out = ROOT / "results"
    out.mkdir(exist_ok=True)
    m = evaluate(load(task)["test"], preds)
    rec = {
        "method": method,
        "task": task,
        "k": k,
        "seed": seed,
        "sec_per_img": round(sec_per_img, 3),
        **m,
        **(extra or {}),
    }
    with open(out / "scores.jsonl", "a") as f:
        f.write(json.dumps(rec) + "\n")
    print(json.dumps(rec), flush=True)
    return rec
