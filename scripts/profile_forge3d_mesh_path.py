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
            numpy_rect_sample = (
                _measure(
                    lambda np=np: _make_numpy_rect_mesh(np, feature_count),
                    runs=args.runs,
                    warmups=args.warmups,
                )
                if vertices_per_feature == 4
                else None
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
                "numpy_rect_median_ms": _round_optional(numpy_rect_sample, "median_ms"),
                "numpy_rect_p95_ms": _round_optional(numpy_rect_sample, "p95_ms"),
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
                f"numpy_rect={_format_optional_ms(row['numpy_rect_median_ms'])} "
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


def _round_optional(sample: dict[str, float] | None, key: str) -> float | None:
    if sample is None:
        return None
    return round(sample[key], 3)


def _format_optional_ms(value: float | None) -> str:
    if value is None:
        return "     n/a"
    return f"{value:8.2f}ms"


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


def _make_numpy_rect_mesh(np: Any, feature_count: int) -> tuple[Any, Any, Any, Any]:
    side = max(1, math.ceil(math.sqrt(feature_count)))
    idx = np.arange(feature_count, dtype=np.float32)
    row = np.floor(idx / side)
    col = idx - row * side
    west, south, east, north = BBOX
    dx = (east - west) / side
    dy = (north - south) / side

    x0 = west + col * dx
    y0 = south + row * dy
    x1 = x0 + dx * 0.82
    y1 = y0 + dy * 0.82
    height = 4.0 + np.mod(row * 13.0 + col * 7.0, 35.0)
    zero = np.zeros_like(height)

    corners = np.empty((feature_count, 8, 3), dtype=np.float32)
    corners[:, 0, :] = np.stack([x0, y0, zero], axis=1)
    corners[:, 1, :] = np.stack([x1, y0, zero], axis=1)
    corners[:, 2, :] = np.stack([x1, y1, zero], axis=1)
    corners[:, 3, :] = np.stack([x0, y1, zero], axis=1)
    corners[:, 4, :] = np.stack([x0, y0, height], axis=1)
    corners[:, 5, :] = np.stack([x1, y0, height], axis=1)
    corners[:, 6, :] = np.stack([x1, y1, height], axis=1)
    corners[:, 7, :] = np.stack([x0, y1, height], axis=1)

    face_corners = np.array(
        [
            0, 1, 2, 3,
            4, 7, 6, 5,
            0, 4, 5, 1,
            1, 5, 6, 2,
            2, 6, 7, 3,
            3, 7, 4, 0,
        ],
        dtype=np.int64,
    )
    positions = corners[:, face_corners, :].reshape(feature_count * 24, 3)

    face_normals = np.array(
        [
            [0, 0, -1],
            [0, 0, 1],
            [0, -1, 0],
            [1, 0, 0],
            [0, 1, 0],
            [-1, 0, 0],
        ],
        dtype=np.float32,
    )
    normals = np.repeat(
        np.repeat(face_normals[None, :, :], feature_count, axis=0),
        4,
        axis=1,
    ).reshape(feature_count * 24, 3)
    uvs = np.tile(
        np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32),
        (feature_count * 6, 1),
    )

    triangle = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint32)
    local_indices = np.concatenate([triangle + 4 * face for face in range(6)]).astype(np.uint32)
    indices = (
        local_indices[None, :] + (np.arange(feature_count, dtype=np.uint32) * 24)[:, None]
    ).reshape(feature_count * 36)

    return positions, normals, uvs, indices


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
