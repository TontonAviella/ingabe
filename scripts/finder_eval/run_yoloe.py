"""YOLOE visual prompts: the marked boxes become a prompt embedding (averaged over reference images); no training."""

import sys
import time
import numpy as np
import torch
import torch.nn.functional as F
from ultralytics import YOLOE
from ultralytics.models.yolo.yoloe import YOLOEVPSegPredictor

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
from common import ROOT, SEEDS, SHOTS, TASKS, done, examples, imgsz, load, save

W = ROOT / "w"


def vpe_of(weights, refs, size):
    vs = []
    for r in refs:
        m = YOLOE(str(W / weights))
        pred = YOLOEVPSegPredictor(
            overrides=dict(
                task=m.model.task, mode="predict", save=False, verbose=False, batch=1, imgsz=size, device="cpu"
            )
        )
        m.model.model[-1].nc = 1
        m.model.names = ["object0"]
        pred.set_prompts({"bboxes": np.array(r["boxes"], dtype=float), "cls": np.zeros(len(r["boxes"]), dtype=int)})
        pred.setup_model(model=m.model)
        vs.append(F.normalize(pred.get_vpe(r["file"]).float(), dim=-1))
    return F.normalize(torch.cat(vs, 0).mean(0, keepdim=True), dim=-1)


def run(weights, task, k, seed):
    if done(weights.replace("-seg.pt", "") + " VP", task, k, seed):
        return
    size = imgsz(task)
    test = load(task)["test"]
    vpe = vpe_of(weights, examples(task, k, seed), size)
    m = YOLOE(str(W / weights))
    m.set_classes(["object0"], vpe)
    preds, t = [], 0.0
    for im in test:
        t0 = time.perf_counter()
        r = m.predict(im["file"], conf=0.01, imgsz=size, device="cpu", verbose=False, max_det=300)[0]
        t += time.perf_counter() - t0
        b = r.boxes
        preds.append([[*map(float, xy), float(s)] for xy, s in zip(b.xyxy.tolist(), b.conf.tolist())])
    save(weights.replace("-seg.pt", "") + " VP", task, k, seed, preds, t / len(test))


if __name__ == "__main__":
    weights = sys.argv[1]
    tasks = sys.argv[2].split(",") if len(sys.argv) > 2 else TASKS
    for task in tasks:
        for k in SHOTS:
            for seed in SEEDS:
                run(weights, task, k, seed)
