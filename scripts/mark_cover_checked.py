#!/usr/bin/env python3
"""Mark a prepared drone photo as one the green cover rule was checked on (src/services/field_taps.py).

Green cover is shown only on such photos: a colour threshold measures how green one camera on one day recorded
leaves, and on another photo the same rule found almost no green where plants stood (plans/PLAN_RWANDA_CROPS.md,
Test 79). Run it only after a pre-registered test of the rule on that photo has passed, and name the test.

    python scripts/mark_cover_checked.py --photo-key <the layer's cog_key> --checked-by "Tests 77 and 78" [--remove]

Uses the bucket named by the S3_* environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.services import field_taps  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--photo-key", required=True, help="the photo's key in object storage (the layer's cog_key)")
    parser.add_argument("--checked-by", help="the test that checked the rule on this photo")
    parser.add_argument("--remove", action="store_true", help="take the mark off: the photo shows no green cover")
    args = parser.parse_args()
    if not args.remove and not (args.checked_by or "").strip():
        print("Name the test that checked the rule on this photo (--checked-by). Nothing written.")
        return 2

    import boto3  # lazy: as in prepare_field_taps.py

    s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
                      aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                      aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                      region_name=os.environ.get("S3_DEFAULT_REGION", "us-east-1"))
    bucket, key = os.environ["S3_BUCKET"], field_taps.cover_rule_key(args.photo_key)
    try:
        s3.head_object(Bucket=bucket, Key=field_taps.manifest_key(args.photo_key))
    except s3.exceptions.ClientError:
        print("This photo is not prepared for field outlines. Nothing written.")
        return 2
    if args.remove:
        s3.delete_object(Bucket=bucket, Key=key)
        print(f"removed {key}: the photo shows no green cover from the next visit")
        return 0
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(field_taps.cover_rule_record(args.checked_by.strip())).encode(),
                  ContentType="application/json")
    print(f"wrote {key}: green cover is measured on this photo from the next visit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
