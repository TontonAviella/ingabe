#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BBOX = [30.0, -2.05, 30.12, -1.93]


@dataclass(frozen=True)
class Sample:
    name: str
    times_ms: list[float]
    peak_kib: float
    detail: dict[str, Any]

    @property
    def mean_ms(self) -> float:
        return statistics.fmean(self.times_ms)

    @property
    def median_ms(self) -> float:
        return statistics.median(self.times_ms)

    @property
    def p95_ms(self) -> float:
        if len(self.times_ms) == 1:
            return self.times_ms[0]
        return statistics.quantiles(self.times_ms, n=20, method="inclusive")[18]


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark Forge3D native paths separately from Python object adapter "
            "overhead: bulk Rust GeoJSON mesh conversion, native scene setup, and "
            "native render frame time."
        )
    )
    parser.add_argument("--features", nargs="+", type=int, default=[16, 256, 1024, 4096])
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument(
        "--render",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run native Scene.render_rgba benchmarks. Use --no-render for geometry-only environments.",
    )
    args = parser.parse_args()

    import forge3d
    import forge3d._forge3d as native
    from forge3d import Scene

    engine_info = _engine_info(native)
    print("Forge3D native benchmark")
    print(f"forge3d_version={getattr(forge3d, '__version__', 'unknown')}")
    print(f"engine={json.dumps(engine_info, separators=(',', ':'), default=str)}")
    print(f"runs={args.runs} warmups={args.warmups} render={args.render}")
    print()

    rows: list[dict[str, Any]] = []
    for feature_count in args.features:
        geojson = _make_grid_geojson(feature_count)
        cube_positions, cube_indices = _cube_mesh()
        transforms = _make_transforms(feature_count)

        samples: list[Sample] = [
            _measure(
                "rust_bulk_geojson_mesh",
                lambda geojson=geojson: _bulk_geojson_mesh(native, geojson),
                runs=args.runs,
                warmups=args.warmups,
            ),
        ]

        if args.render:
            setup_sample = _measure(
                "rust_scene_setup_instances",
                lambda: _scene_setup(
                    Scene,
                    cube_positions,
                    cube_indices,
                    transforms,
                    width=args.width,
                    height=args.height,
                ),
                runs=args.runs,
                warmups=args.warmups,
            )
            samples.append(setup_sample)
            if setup_sample.detail.get("status") == "failed":
                samples.append(
                    Sample(
                        name="rust_scene_render_rgba",
                        times_ms=[0.0],
                        peak_kib=0.0,
                        detail={
                            "status": "failed",
                            "error_type": "RendererSetupFailed",
                            "error": "Scene setup failed; render benchmark skipped.",
                        },
                    )
                )
            else:
                try:
                    scene = _make_scene(
                        Scene,
                        cube_positions,
                        cube_indices,
                        transforms,
                        width=args.width,
                        height=args.height,
                    )
                except BaseException as exc:
                    samples.append(
                        Sample(
                            name="rust_scene_render_rgba",
                            times_ms=[0.0],
                            peak_kib=0.0,
                            detail={
                                "status": "failed",
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                        )
                    )
                else:
                    samples.append(
                        _measure(
                            "rust_scene_render_rgba",
                            lambda scene=scene: _render_scene(scene),
                            runs=args.runs,
                            warmups=args.warmups,
                        )
                    )

        for sample in samples:
            row = {
                "features": feature_count,
                "path": sample.name,
                "mean_ms": round(sample.mean_ms, 3),
                "median_ms": round(sample.median_ms, 3),
                "p95_ms": round(sample.p95_ms, 3),
                "peak_kib": round(sample.peak_kib, 1),
                "detail": sample.detail,
                "engine": engine_info,
            }
            rows.append(row)
            print(
                f"{feature_count:5d} {sample.name:28s} "
                f"median={sample.median_ms:9.2f}ms "
                f"p95={sample.p95_ms:9.2f}ms "
                f"peak={sample.peak_kib:9.1f}KiB "
                f"{_detail_summary(sample.detail)}"
            )
        print()

    print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))


def _measure(
    name: str,
    fn: Callable[[], dict[str, Any]],
    *,
    runs: int,
    warmups: int,
) -> Sample:
    try:
        for _ in range(warmups):
            fn()
    except BaseException as exc:
        return Sample(
            name=name,
            times_ms=[0.0],
            peak_kib=0.0,
            detail={
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )

    times_ms: list[float] = []
    peak_kib = 0.0
    detail: dict[str, Any] = {}
    for _ in range(runs):
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        try:
            detail = fn()
            elapsed = (time.perf_counter() - start) * 1000.0
            _, peak = tracemalloc.get_traced_memory()
        except BaseException as exc:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return Sample(
                name=name,
                times_ms=times_ms or [0.0],
                peak_kib=max(peak_kib, peak / 1024.0),
                detail={
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
        tracemalloc.stop()
        times_ms.append(elapsed)
        peak_kib = max(peak_kib, peak / 1024.0)
    return Sample(name=name, times_ms=times_ms, peak_kib=peak_kib, detail=detail)


def _engine_info(native: Any) -> dict[str, Any]:
    try:
        info = native.engine_info() if hasattr(native, "engine_info") else {}
    except BaseException as exc:
        return {"status": "error", "error": str(exc)}
    if isinstance(info, dict):
        return info
    return {"status": "unknown", "raw": str(info)}


def _bulk_geojson_mesh(native: Any, geojson: str) -> dict[str, Any]:
    out = native.import_osm_buildings_from_geojson_py(geojson, 10.0, "height")
    return {
        "vertex_count": int(out.get("vertex_count") or len(out.get("positions", []))),
        "triangle_count": int(out.get("triangle_count") or len(out.get("indices", [])) // 3),
        "positions_len": len(out.get("positions", [])),
        "indices_len": len(out.get("indices", [])),
    }


def _scene_setup(
    scene_cls: Any,
    positions: np.ndarray,
    indices: np.ndarray,
    transforms: np.ndarray,
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    scene = _make_scene(scene_cls, positions, indices, transforms, width=width, height=height)
    stats = scene.get_stats() if hasattr(scene, "get_stats") else {}
    return {
        "instance_count": int(transforms.shape[0]),
        "width": width,
        "height": height,
        "stats": stats,
    }


def _make_scene(
    scene_cls: Any,
    positions: np.ndarray,
    indices: np.ndarray,
    transforms: np.ndarray,
    *,
    width: int,
    height: int,
) -> Any:
    scene = scene_cls(int(width), int(height), grid=32)
    scene.disable_terrain()
    if hasattr(scene, "disable_ground_plane"):
        scene.disable_ground_plane()
    scene.set_camera_look_at((0.0, 10.0, 22.0), (0.0, 0.0, 0.0), (0.0, 1.0, 0.0), 45.0, 0.1, 200.0)
    scene.add_instanced_mesh(
        positions,
        indices,
        transforms,
        color=(0.9, 0.35, 0.12, 1.0),
        light_dir=(0.25, 0.75, 0.35),
        light_intensity=1.15,
    )
    return scene


def _render_scene(scene: Any) -> dict[str, Any]:
    image = np.asarray(scene.render_rgba())
    alpha_sum = int(image[..., 3].sum()) if image.ndim == 3 and image.shape[2] == 4 else 0
    return {
        "shape": tuple(int(x) for x in image.shape),
        "dtype": str(image.dtype),
        "alpha_sum": alpha_sum,
    }


def _make_grid_geojson(target_features: int) -> str:
    side = max(1, int(target_features**0.5))
    while side * side < target_features:
        side += 1

    west, south, east, north = BBOX
    dx = (east - west) / side
    dy = (north - south) / side
    features = []
    for row in range(side):
        for col in range(side):
            if len(features) >= target_features:
                break
            x0 = west + col * dx
            x1 = x0 + dx * 0.82
            y0 = south + row * dy
            y1 = y0 + dy * 0.82
            height = 4.0 + float((row * 13 + col * 7) % 35)
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[
                            [x0, y0],
                            [x1, y0],
                            [x1, y1],
                            [x0, y1],
                            [x0, y0],
                        ]],
                    },
                    "properties": {
                        "id": f"impact-{row}-{col}",
                        "height": height,
                        "risk_score": min(100.0, height * 2.5),
                    },
                }
            )
    return json.dumps({"type": "FeatureCollection", "features": features})


def _cube_mesh() -> tuple[np.ndarray, np.ndarray]:
    positions = np.array(
        [
            [-0.5, -0.5, -0.5],
            [0.5, -0.5, -0.5],
            [0.5, 0.5, -0.5],
            [-0.5, 0.5, -0.5],
            [-0.5, -0.5, 0.5],
            [0.5, -0.5, 0.5],
            [0.5, 0.5, 0.5],
            [-0.5, 0.5, 0.5],
        ],
        dtype=np.float32,
    )
    indices = np.array(
        [
            [0, 1, 2],
            [0, 2, 3],
            [4, 6, 5],
            [4, 7, 6],
            [0, 4, 5],
            [0, 5, 1],
            [1, 5, 6],
            [1, 6, 2],
            [2, 6, 7],
            [2, 7, 3],
            [3, 7, 4],
            [3, 4, 0],
        ],
        dtype=np.uint32,
    )
    return positions, indices


def _make_transforms(count: int) -> np.ndarray:
    side = max(1, int(count**0.5))
    while side * side < count:
        side += 1
    spacing = 18.0 / max(1, side - 1)
    transforms = []
    for idx in range(count):
        row = idx // side
        col = idx % side
        height = 0.35 + ((idx * 37) % 100) / 85.0
        transform = np.eye(4, dtype=np.float32)
        transform[0, 0] = max(0.06, spacing * 0.28)
        transform[1, 1] = height
        transform[2, 2] = max(0.06, spacing * 0.28)
        transform[0, 3] = (col - (side - 1) / 2.0) * spacing
        transform[1, 3] = height * 0.5
        transform[2, 3] = (row - (side - 1) / 2.0) * spacing
        transforms.append(transform.reshape(-1))
    return np.ascontiguousarray(np.stack(transforms).astype(np.float32))


def _detail_summary(detail: dict[str, Any]) -> str:
    if detail.get("status") == "failed":
        return f"failed={detail.get('error_type')} {str(detail.get('error'))[:120]}"
    if "triangle_count" in detail:
        return f"vertices={detail['vertex_count']} triangles={detail['triangle_count']}"
    if "instance_count" in detail:
        return f"instances={detail['instance_count']} {detail['width']}x{detail['height']}"
    if "shape" in detail:
        return f"shape={detail['shape']} alpha_sum={detail['alpha_sum']}"
    return ""


if __name__ == "__main__":
    main()
