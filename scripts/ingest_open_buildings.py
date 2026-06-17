#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import gzip
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from shapely.geometry import mapping, shape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.open_buildings import (  # noqa: E402
    bbox_geometry,
    open_buildings_row_geometry,
    parse_number,
    select_open_buildings_tiles_for_bbox,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract Google Open Buildings polygons for a bbox. This is an "
            "offline/cache ingest helper, not a Sage live-response path."
        )
    )
    parser.add_argument("--bbox", required=True, help="west,south,east,north in WGS84")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--max-buildings", type=int, default=200_000)
    parser.add_argument("--out-geojson", default="data/open_buildings/extract.geojson")
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Only print intersecting Open Buildings tile URLs; do not download tile CSVs.",
    )
    args = parser.parse_args()

    bbox = _parse_bbox(args.bbox)
    tiles = select_open_buildings_tiles_for_bbox(bbox)
    if args.metadata_only:
        print(json.dumps({"bbox": bbox, "candidate_tile_count": len(tiles), "candidate_tiles": tiles}, indent=2))
        return 0

    out_path = Path(args.out_geojson)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    query_geom = bbox_geometry(bbox)
    count = 0

    with out_path.open("w", encoding="utf-8") as output:
        output.write('{"type":"FeatureCollection","features":[\n')
        first = True
        for tile in tiles:
            tile_url = tile.get("tile_url")
            if not tile_url:
                continue
            for feature in _iter_tile_features(str(tile_url), query_geom, args.min_confidence):
                if not first:
                    output.write(",\n")
                output.write(json.dumps(feature, separators=(",", ":")))
                first = False
                count += 1
                if count >= args.max_buildings:
                    break
            if count >= args.max_buildings:
                break
        output.write("\n]}\n")

    print(
        json.dumps(
            {
                "status": "success",
                "bbox": bbox,
                "candidate_tile_count": len(tiles),
                "building_count": count,
                "out_geojson": str(out_path),
            },
            indent=2,
        )
    )
    return 0


def _iter_tile_features(tile_url: str, query_geom, min_confidence: float):
    try:
        with urllib.request.urlopen(tile_url, timeout=120) as response:
            with gzip.GzipFile(fileobj=response) as gz:
                text = (line.decode("utf-8") for line in gz)
                reader = csv.DictReader(text)
                for row in reader:
                    confidence = parse_number(row.get("confidence"))
                    if confidence is not None and confidence < min_confidence:
                        continue
                    geom = open_buildings_row_geometry(row)
                    if geom is None or geom.is_empty:
                        continue
                    if not geom.is_valid:
                        geom = geom.buffer(0)
                    if geom.is_empty or not geom.intersects(query_geom):
                        continue
                    yield {
                        "type": "Feature",
                        "geometry": mapping(geom),
                        "properties": {
                            "source": "open_buildings_v3",
                            "confidence": confidence,
                            "area_in_meters": parse_number(row.get("area_in_meters")),
                            "full_plus_code": row.get("full_plus_code"),
                        },
                    }
    except (
        OSError,
        TimeoutError,
        UnicodeDecodeError,
        urllib.error.HTTPError,
        urllib.error.URLError,
        socket.timeout,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "tile_download_failed",
                    "tile_url": tile_url,
                    "error": str(exc),
                },
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return


def _parse_bbox(raw: str) -> list[float]:
    try:
        bbox = [float(part.strip()) for part in raw.split(",")]
    except ValueError as exc:
        raise SystemExit(f"Invalid bbox {raw!r}: {exc}") from exc
    if len(bbox) != 4:
        raise SystemExit("bbox must contain four comma-separated numbers")
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise SystemExit("bbox must be ordered west,south,east,north")
    return bbox


if __name__ == "__main__":
    raise SystemExit(main())
