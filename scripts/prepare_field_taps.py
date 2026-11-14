#!/usr/bin/env python3
"""Prepare a drone photo for field outlines from taps (src/services/field_taps.py).

Runs on our own machine, never in the app: the heavy half of Segment Anything ViT-H over the photo's grid of
windows, one stored array per window plus the manifest. About 15 s a window on an M1 (6 threads), 2 MB a window.

    python scripts/prepare_field_taps.py --cog photo.cog.tif --photo-key <the layer's cog_key> \
        --weights sam_vit_h_4b8939.pth --out prepared/ [--upload]

The files are written under --out at the same keys they have in object storage, so a stopped run carries on where
it stopped. --upload then puts them into the bucket named by the S3_* environment variables (and the tap model, if
--tap-model is given). Needs torch and segment_anything, which the app image does not have.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import from_bounds

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.services import field_taps, stored_files  # noqa: E402

MIN_READABLE = 0.05  # a window with less of the photo than this is left out


def read_window(ds: rasterio.DatasetReader, grid: field_taps.Grid, window: tuple[int, int]) -> np.ndarray:
    """The window's picture as the tests read it: RGB, WINDOW_PX square, averaged, empty outside the photo."""
    cx, cy = grid.centre(window)
    half = field_taps.WINDOW_M / 2
    size = field_taps.WINDOW_PX
    pixels = ds.read([1, 2, 3], window=from_bounds(cx - half, cy - half, cx + half, cy + half, ds.transform),
                     out_shape=(3, size, size), resampling=Resampling.average, boundless=True, fill_value=0)
    return np.ascontiguousarray(pixels.transpose(1, 2, 0))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cog", required=True, help="the photo (a local path or a URL rasterio can open)")
    parser.add_argument("--photo-key", required=True, help="the photo's key in object storage (the layer's cog_key)")
    parser.add_argument("--weights", required=True, help="sam_vit_h_4b8939.pth")
    parser.add_argument("--out", required=True, help="folder to write into")
    parser.add_argument("--threads", type=int, default=6)
    parser.add_argument("--limit", type=int, default=0, help="prepare at most this many new windows (0: all)")
    parser.add_argument("--tap-model", help="the exported tap model (.onnx) to upload with the windows")
    parser.add_argument("--upload", action="store_true", help="put everything under --out into object storage")
    args = parser.parse_args()

    with rasterio.open(args.cog) as ds:
        if ds.crs is None or not ds.crs.is_projected or ds.crs.linear_units.lower() not in {"metre", "meter"}:
            print(f"The photo's coordinates must be in metres; this one is {ds.crs}. Nothing prepared.")
            return 2
        left, bottom, right, top = ds.bounds
        grid = field_taps.Grid(crs=ds.crs.to_string(), origin_x=left, origin_y=top, windows=frozenset())
        candidates = field_taps.candidate_windows(left, bottom, right, top)
        manifest_path = os.path.join(args.out, field_taps.manifest_key(args.photo_key))
        os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
        done: list[tuple[int, int]] = []
        skipped_path = os.path.join(os.path.dirname(manifest_path), "empty_windows.json")
        empty = {tuple(w) for w in json.load(open(skipped_path))} if os.path.exists(skipped_path) else set()
        todo = []
        for window in candidates:
            if window in empty:
                continue
            (done if os.path.exists(os.path.join(args.out, field_taps.window_key(args.photo_key, window)))
             else todo).append(window)
        print(f"{len(candidates)} windows touch the photo: {len(done)} prepared, {len(empty)} empty, {len(todo)} to do")

        predictor = None
        new = 0
        for window in todo:
            if args.limit and new >= args.limit:
                break
            picture = read_window(ds, grid, window)
            if (picture.max(axis=2) > 0).mean() < MIN_READABLE:
                empty.add(window)
                continue
            if predictor is None:
                import torch  # lazy: only this script needs torch
                from segment_anything import SamPredictor, sam_model_registry  # lazy: as above

                torch.set_num_threads(args.threads)
                predictor = SamPredictor(sam_model_registry["vit_h"](checkpoint=args.weights).eval())
            started = time.time()
            predictor.set_image(picture)
            array = predictor.get_image_embedding().numpy().astype(np.float16)
            path = os.path.join(args.out, field_taps.window_key(args.photo_key, window))
            np.save(path + ".part.npy", array)
            os.replace(path + ".part.npy", path)
            done.append(window)
            new += 1
            print(f"window {window}: {time.time() - started:.1f} s ({len(done)} prepared)", flush=True)
        json.dump(sorted(empty), open(skipped_path, "w"))

    left_over = len(candidates) - len(done) - len(empty)
    if left_over:
        print(f"{left_over} windows still to do; run again to carry on. No manifest written yet.")
        return 1
    record = field_taps.manifest(grid.crs, grid.origin_x, grid.origin_y, sorted(done))
    json.dump(record, open(manifest_path, "w"))
    print(f"manifest written: {len(done)} windows")
    if args.upload:
        return upload(args.out, args.photo_key, args.tap_model)
    return 0


def upload(out: str, photo_key: str, tap_model: str | None) -> int:
    import boto3  # lazy: only with --upload

    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
                      aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                      region_name=os.environ.get("S3_DEFAULT_REGION", "us-east-1"))
    bucket = os.environ["S3_BUCKET"]
    if tap_model:
        if stored_files.file_sha256(tap_model) != field_taps.TAP_MODEL_SHA256:
            print("The tap model file is not the one this app answers taps with (sha256 differs). Nothing uploaded.")
            return 2
        s3.upload_file(tap_model, bucket, field_taps.TAP_MODEL_KEY)
    folder = os.path.dirname(os.path.join(out, field_taps.manifest_key(photo_key)))
    names = sorted(n for n in os.listdir(folder) if n.endswith(".npy"))
    for name in names:
        with open(os.path.join(folder, name), "rb") as f:
            s3.upload_fileobj(io.BytesIO(f.read()), bucket, f"{os.path.dirname(field_taps.manifest_key(photo_key))}/{name}")
    # the manifest goes last: the app treats a photo as prepared only once every window is there
    s3.upload_file(os.path.join(folder, "manifest.json"), bucket, field_taps.manifest_key(photo_key))
    print(f"uploaded {len(names)} windows and the manifest to {bucket}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
