"""SAM 3 baseline for run_sameimage.py (dropped 2026-10-11: AP50 0.00-0.04). Calls the sam3.c CLI (github.com/NetraRuntime/sam3.c,
built with the EfficientViT-B1 config patch described in the README); on one picture with box and/or text prompts; returns [[x0,y0,x1,y1,score]] and seconds."""

import json, os, subprocess, time, tempfile
from pathlib import Path

ROOT = Path(
    os.environ.get("SAM3C_DIR", Path(__file__).resolve().parent / "work" / "sam3c")
)  # a sam3.c checkout + weights
CLI = ROOT / "src/build/sam3_cli"
WEIGHTS = ROOT / "weights"


def detect(image, boxes=(), text=None, model="tinyvit", prob=0.05):
    args = [
        str(CLI),
        "segment",
        "-q",
        "-m",
        str(WEIGHTS / f"{model}.sam3"),
        "-i",
        str(image),
        "--json",
        "--nms-prob-thresh",
        str(prob),
        "-o",
        tempfile.gettempdir(),
    ]
    for b in boxes:
        args += ["-b", ",".join(str(int(round(v))) for v in b[:4])]
    if text:
        args += ["-t", text]
    t0 = time.perf_counter()
    r = subprocess.run(args, capture_output=True, text=True)
    dt = time.perf_counter() - t0
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else f"exit {r.returncode}")
    d = json.loads(r.stdout)
    return [[*m["box"], m.get("iou_score", 0.0)] for m in d["masks"] if "box" in m], dt
