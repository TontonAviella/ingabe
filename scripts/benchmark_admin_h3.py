#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import math
import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import h3
from shapely.geometry import mapping, shape

from src.services.admin_h3 import AdminH3Options, admin_geojson_to_h3, h3_cell_geojson_geometry


@dataclass(frozen=True)
class Case:
    name: str
    admin_level: str
    resolution: int
    width_deg: float
    height_deg: float
    vertices: int


@dataclass(frozen=True)
class Sample:
    name: str
    times_ms: list[float]
    detail: dict[str, Any]

    @property
    def median_ms(self) -> float:
        return statistics.median(self.times_ms)

    @property
    def p95_ms(self) -> float:
        if len(self.times_ms) == 1:
            return self.times_ms[0]
        return statistics.quantiles(self.times_ms, n=20, method="inclusive")[18]


CASES = [
    Case("district_r7", "district", 7, 0.34, 0.24, 96),
    Case("sector_r8", "sector", 8, 0.11, 0.08, 72),
    Case("admin_cell_r9", "admin_cell", 9, 0.038, 0.028, 48),
    Case("village_r10", "village", 10, 0.014, 0.010, 32),
]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Rwanda admin-boundary H3 polyfill paths: raw admin GeoJSON, "
            "existing bbox H3 grid style, simple H3 polyfill, and full overlap-aware "
            "admin crosswalk."
        )
    )
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--max-hexes", type=int, default=50_000)
    args = parser.parse_args()

    print("Admin boundary H3 benchmark")
    print(f"h3_version={getattr(h3, '__version__', 'unknown')}")
    print(f"runs={args.runs} warmups={args.warmups}")
    print()

    rows: list[dict[str, Any]] = []
    for case in CASES:
        geojson = _make_admin_geojson(case)
        geom = shape(geojson["features"][0]["geometry"])
        bbox = geom.bounds
        input_bytes = len(_json_dumps(geojson).encode("utf-8"))

        samples = [
            _measure("raw_admin_geojson_dump", lambda geojson=geojson: _raw_dump(geojson), args),
            _measure(
                "h3_cell_ids_only",
                lambda geom=geom, case=case: _h3_ids_only(geom, case.resolution),
                args,
            ),
            _measure(
                "existing_bbox_h3_grid_geojson",
                lambda bbox=bbox, case=case: _bbox_grid_geojson(bbox, case.resolution),
                args,
            ),
            _measure(
                "simple_polyfill_hex_geojson",
                lambda geom=geom, case=case: _simple_polyfill_geojson(geom, case.resolution),
                args,
            ),
            _measure(
                "admin_h3_crosswalk_overlap",
                lambda geojson=geojson, case=case: _admin_polyfill(
                    geojson,
                    case,
                    max_hexes=args.max_hexes,
                    include_geometry=False,
                    serialize=False,
                ),
                args,
            ),
            _measure(
                "admin_h3_crosswalk_json_payload",
                lambda geojson=geojson, case=case: _admin_polyfill(
                    geojson,
                    case,
                    max_hexes=args.max_hexes,
                    include_geometry=False,
                    serialize=True,
                ),
                args,
            ),
            _measure(
                "admin_h3_full_overlap_layer",
                lambda geojson=geojson, case=case: _admin_polyfill(
                    geojson,
                    case,
                    max_hexes=args.max_hexes,
                    include_geometry=True,
                    serialize=False,
                ),
                args,
            ),
            _measure(
                "admin_h3_full_overlap_json_payload",
                lambda geojson=geojson, case=case: _admin_polyfill(
                    geojson,
                    case,
                    max_hexes=args.max_hexes,
                    include_geometry=True,
                    serialize=True,
                ),
                args,
            ),
        ]

        print(
            f"{case.name} level={case.admin_level} res={case.resolution} "
            f"input_vertices={case.vertices} input={input_bytes}B"
        )
        for sample in samples:
            row = {
                "case": case.name,
                "admin_level": case.admin_level,
                "resolution": case.resolution,
                "input_vertices": case.vertices,
                "input_bytes": input_bytes,
                "path": sample.name,
                "median_ms": round(sample.median_ms, 3),
                "p95_ms": round(sample.p95_ms, 3),
                "detail": sample.detail,
            }
            rows.append(row)
            print(
                f"  {sample.name:32s} median={sample.median_ms:8.3f}ms "
                f"p95={sample.p95_ms:8.3f}ms {_detail_summary(sample.detail)}"
            )
        print()

    print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))


def _measure(name: str, fn: Callable[[], dict[str, Any]], args: argparse.Namespace) -> Sample:
    for _ in range(args.warmups):
        fn()

    times_ms: list[float] = []
    detail: dict[str, Any] = {}
    for _ in range(args.runs):
        gc.collect()
        start = time.perf_counter()
        detail = fn()
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return Sample(name=name, times_ms=times_ms, detail=detail)


def _make_admin_geojson(case: Case) -> dict[str, Any]:
    center_lng = 30.05
    center_lat = -1.95
    rx = case.width_deg / 2.0
    ry = case.height_deg / 2.0
    coords = []
    for idx in range(case.vertices):
        angle = (2.0 * math.pi * idx) / case.vertices
        wobble = 1.0 + 0.11 * math.sin(angle * 3.0) + 0.06 * math.cos(angle * 7.0)
        coords.append(
            [
                center_lng + math.cos(angle) * rx * wobble,
                center_lat + math.sin(angle) * ry * wobble,
            ]
        )
    coords.append(coords[0])
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    f"{case.admin_level}_id": f"{case.admin_level}-demo",
                    f"{case.admin_level}_name": f"Demo {case.admin_level}",
                },
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            }
        ],
    }


def _raw_dump(geojson: dict[str, Any]) -> dict[str, Any]:
    payload = _json_dumps(geojson)
    return {"bytes": len(payload.encode("utf-8")), "features": len(geojson.get("features") or [])}


def _h3_ids_only(geom: Any, resolution: int) -> dict[str, Any]:
    hex_ids = h3.geo_to_cells(mapping(geom), res=resolution)
    return {"hexes": len(hex_ids)}


def _bbox_grid_geojson(bounds: tuple[float, float, float, float], resolution: int) -> dict[str, Any]:
    west, south, east, north = bounds
    boundary_polygon = {
        "type": "Polygon",
        "coordinates": [[
            [west, south],
            [east, south],
            [east, north],
            [west, north],
            [west, south],
        ]],
    }
    hex_ids = h3.geo_to_cells(boundary_polygon, res=resolution)
    features = [
        {
            "type": "Feature",
            "properties": {"h3_index": h3_index, "resolution": resolution},
            "geometry": h3_cell_geojson_geometry(h3_index),
        }
        for h3_index in sorted(hex_ids)
    ]
    payload = {"type": "FeatureCollection", "features": features}
    return {"hexes": len(features), "bytes": len(_json_dumps(payload).encode("utf-8"))}


def _simple_polyfill_geojson(geom: Any, resolution: int) -> dict[str, Any]:
    hex_ids = h3.geo_to_cells(mapping(geom), res=resolution)
    features = [
        {
            "type": "Feature",
            "properties": {"h3_index": h3_index, "resolution": resolution},
            "geometry": h3_cell_geojson_geometry(h3_index),
        }
        for h3_index in sorted(hex_ids)
    ]
    payload = {"type": "FeatureCollection", "features": features}
    return {"hexes": len(features), "bytes": len(_json_dumps(payload).encode("utf-8"))}


def _admin_polyfill(
    geojson: dict[str, Any],
    case: Case,
    *,
    max_hexes: int,
    include_geometry: bool,
    serialize: bool,
) -> dict[str, Any]:
    result = admin_geojson_to_h3(
        geojson,
        options=AdminH3Options(
            resolution=case.resolution,
            admin_level=case.admin_level,
            id_property=f"{case.admin_level}_id",
            name_property=f"{case.admin_level}_name",
            max_hexes=max_hexes,
            include_geometry=include_geometry,
        ),
    )
    detail = {
        "hexes": result["metadata"]["feature_count"],
        "geometry": include_geometry,
    }
    if serialize:
        detail["bytes"] = len(_json_dumps(result).encode("utf-8"))
    return detail


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _detail_summary(detail: dict[str, Any]) -> str:
    parts = []
    if "hexes" in detail:
        parts.append(f"hexes={detail['hexes']}")
    if "features" in detail:
        parts.append(f"features={detail['features']}")
    if "bytes" in detail:
        parts.append(f"bytes={detail['bytes']}B")
    return " ".join(parts)


if __name__ == "__main__":
    main()
