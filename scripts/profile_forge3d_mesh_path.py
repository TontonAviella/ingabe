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
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BBOX = [30.0, -2.05, 30.12, -1.93]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Profile the fast Forge3D native mesh path and its slowdown factors: "
            "GeoJSON build/dump, native Rust mesh conversion, feature count, and "
            "polygon vertex count."
        )
    )
    parser.add_argument("--features", nargs="+", type=int, default=[16, 256, 1024, 4096])
    parser.add_argument("--vertices-per-feature", nargs="+", type=int, default=[4, 8, 16])
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=5)
    args = parser.parse_args()

    import forge3d
    import forge3d._forge3d as native
    import numpy as np

    engine_info = _engine_info(native)
    print("Forge3D fast mesh path profile")
    print(f"forge3d_version={getattr(forge3d, '__version__', 'unknown')}")
    print(f"engine={json.dumps(engine_info, separators=(',', ':'), default=str)}")
    print(f"runs={args.runs} warmups={args.warmups}")
    print()

    rows: list[dict[str, Any]] = []
    for vertices_per_feature in args.vertices_per_feature:
        print(f"vertices_per_feature={vertices_per_feature}")
        for feature_count in args.features:
            collection = _make_feature_collection(feature_count, vertices_per_feature)
            geojson_sample = _json_dumps(collection)
            output_sample = native.import_osm_buildings_from_geojson_py(
                geojson_sample, 10.0, "height"
            )
            array_features = _make_array_features(np, feature_count, vertices_per_feature)
            output_bytes = _mesh_output_bytes(output_sample)

            build_sample = _measure(
                lambda collection=collection: _json_dumps(collection),
                runs=args.runs,
                warmups=args.warmups,
            )
            parse_sample = _measure(
                lambda geojson=geojson_sample: json.loads(geojson),
                runs=args.runs,
                warmups=args.warmups,
            )
            array_build_sample = _measure(
                lambda np=np: _make_array_features(np, feature_count, vertices_per_feature),
                runs=args.runs,
                warmups=args.warmups,
            )
            native_sample = _measure(
                lambda geojson=geojson_sample: native.import_osm_buildings_from_geojson_py(
                    geojson, 10.0, "height"
                ),
                runs=args.runs,
                warmups=args.warmups,
            )
            native_array_sample = _measure(
                lambda array_features=array_features: native.import_osm_buildings_extrude_py(
                    array_features, 10.0, "height"
                ),
                runs=args.runs,
                warmups=args.warmups,
            )
            native_with_gc_sample = _measure(
                lambda geojson=geojson_sample: native.import_osm_buildings_from_geojson_py(
                    geojson, 10.0, "height"
                ),
                runs=args.runs,
                warmups=args.warmups,
                collect_before_each=True,
            )

            row = {
                "features": feature_count,
                "vertices_per_feature": vertices_per_feature,
                "geojson_bytes": len(geojson_sample.encode("utf-8")),
                "output_bytes": output_bytes,
                "vertex_count": int(output_sample.get("vertex_count", 0)),
                "triangle_count": int(output_sample.get("triangle_count", 0)),
                "json_dump_median_ms": round(build_sample["median_ms"], 3),
                "json_load_median_ms": round(parse_sample["median_ms"], 3),
                "array_build_median_ms": round(array_build_sample["median_ms"], 3),
                "native_median_ms": round(native_sample["median_ms"], 3),
                "native_p95_ms": round(native_sample["p95_ms"], 3),
                "native_array_median_ms": round(native_array_sample["median_ms"], 3),
                "native_array_p95_ms": round(native_array_sample["p95_ms"], 3),
                "native_with_gc_median_ms": round(native_with_gc_sample["median_ms"], 3),
            }
            rows.append(row)
            print(
                f"{feature_count:6d} "
                f"geojson={row['geojson_bytes']:9d}B "
                f"mesh={row['output_bytes']:9d}B "
                f"json_dump={row['json_dump_median_ms']:8.2f}ms "
                f"json_load={row['json_load_median_ms']:8.2f}ms "
                f"array_build={row['array_build_median_ms']:8.2f}ms "
                f"native_geojson={row['native_median_ms']:8.2f}ms "
                f"native_array={row['native_array_median_ms']:8.2f}ms "
                f"p95={row['native_p95_ms']:8.2f}ms "
                f"native+gc={row['native_with_gc_median_ms']:8.2f}ms "
                f"verts={row['vertex_count']} tris={row['triangle_count']}"
            )
        print()

    print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))


def _measure(
    fn: Callable[[], Any],
    *,
    runs: int,
    warmups: int,
    collect_before_each: bool = False,
) -> dict[str, float]:
    for _ in range(warmups):
        fn()

    times_ms: list[float] = []
    for _ in range(runs):
        if collect_before_each:
            gc.collect()
        start = time.perf_counter()
        fn()
        times_ms.append((time.perf_counter() - start) * 1000.0)

    return {
        "mean_ms": statistics.fmean(times_ms),
        "median_ms": statistics.median(times_ms),
        "p95_ms": times_ms[0]
        if len(times_ms) == 1
        else statistics.quantiles(times_ms, n=20, method="inclusive")[18],
    }


def _engine_info(native: Any) -> dict[str, Any]:
    try:
        info = native.engine_info() if hasattr(native, "engine_info") else {}
    except BaseException as exc:
        return {"status": "error", "error": str(exc)}
    if isinstance(info, dict):
        return info
    return {"status": "unknown", "raw": str(info)}


def _json_dumps(collection: dict[str, Any]) -> str:
    return json.dumps(collection, separators=(",", ":"))


def _mesh_output_bytes(mesh: dict[str, Any]) -> int:
    total = 0
    for key in ("positions", "normals", "uvs", "tangents", "indices"):
        value = mesh.get(key)
        if hasattr(value, "nbytes"):
            total += int(value.nbytes)
    return total


def _make_feature_collection(feature_count: int, vertices_per_feature: int) -> dict[str, Any]:
    side = max(1, math.ceil(math.sqrt(feature_count)))
    west, south, east, north = BBOX
    dx = (east - west) / side
    dy = (north - south) / side
    features = []

    for idx in range(feature_count):
        row = idx // side
        col = idx % side
        cx = west + (col + 0.5) * dx
        cy = south + (row + 0.5) * dy
        rx = dx * 0.41
        ry = dy * 0.41
        ring = _polygon_ring(cx, cy, rx, ry, vertices_per_feature)
        height = 4.0 + float((row * 13 + col * 7) % 35)
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [ring],
                },
                "properties": {
                    "height": height,
                    "risk_score": min(100.0, height * 2.5),
                },
            }
        )

    return {"type": "FeatureCollection", "features": features}


def _make_array_features(
    np: Any, feature_count: int, vertices_per_feature: int
) -> list[dict[str, Any]]:
    side = max(1, math.ceil(math.sqrt(feature_count)))
    west, south, east, north = BBOX
    dx = (east - west) / side
    dy = (north - south) / side
    features = []

    for idx in range(feature_count):
        row = idx // side
        col = idx % side
        cx = west + (col + 0.5) * dx
        cy = south + (row + 0.5) * dy
        rx = dx * 0.41
        ry = dy * 0.41
        height = 4.0 + float((row * 13 + col * 7) % 35)
        features.append(
            {
                "coords": np.asarray(
                    _polygon_ring(cx, cy, rx, ry, vertices_per_feature),
                    dtype=np.float32,
                ),
                "height": height,
            }
        )

    return features


def _polygon_ring(cx: float, cy: float, rx: float, ry: float, vertices: int) -> list[list[float]]:
    vertices = max(4, vertices)
    if vertices == 4:
        ring = [
            [cx - rx, cy - ry],
            [cx + rx, cy - ry],
            [cx + rx, cy + ry],
            [cx - rx, cy + ry],
        ]
    else:
        ring = [
            [
                cx + math.cos((2.0 * math.pi * i) / vertices) * rx,
                cy + math.sin((2.0 * math.pi * i) / vertices) * ry,
            ]
            for i in range(vertices)
        ]
    ring.append(ring[0])
    return ring


if __name__ == "__main__":
    main()
