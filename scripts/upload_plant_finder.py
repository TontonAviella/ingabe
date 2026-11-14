#!/usr/bin/env python3
"""Put the plant finder's three model files into object storage (src/services/plant_finder.py).

    python scripts/upload_plant_finder.py --bundle <folder with student.onnx, dino_small.onnx, rule.npz>

Each file is checked against the sha256 the app expects before anything is uploaded, so the app can only ever run
the models that were tested. Uses the bucket named by the S3_* environment variables.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.services import plant_finder, stored_files  # noqa: E402

FILES = (("student.onnx", plant_finder.STUDENT_KEY, plant_finder.STUDENT_SHA256),
         ("dino_small.onnx", plant_finder.PICTURE_KEY, plant_finder.PICTURE_SHA256),
         ("rule.npz", plant_finder.RULE_KEY, plant_finder.RULE_SHA256))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", required=True, help="the folder holding the three files")
    args = parser.parse_args()
    for name, _, sha256 in FILES:
        path = os.path.join(args.bundle, name)
        if not os.path.exists(path) or stored_files.file_sha256(path) != sha256:
            print(f"{path} is missing or is not the file the app expects (sha256 differs). Nothing uploaded.")
            return 2

    import boto3  # lazy: as in prepare_field_taps.py

    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
                      aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                      region_name=os.environ.get("S3_DEFAULT_REGION", "us-east-1"))
    for name, key, _ in FILES:
        s3.upload_file(os.path.join(args.bundle, name), os.environ["S3_BUCKET"], key)
        print(f"uploaded {name} -> {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
