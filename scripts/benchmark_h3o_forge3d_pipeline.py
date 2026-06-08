#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import statistics
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES = ("district_r7", "sector_r8", "admin_cell_r9", "village_r10")
RUST_CRATE = ROOT / "scripts" / "admin_h3_rust_bench"
RUST_BIN = RUST_CRATE / "target" / "release" / "emit_overlap_geojson"


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark the combined h3o + Forge3D path: Rust h3o/geo overlap "
            "emits GeoJSON, then Forge3D native builds the extrusion mesh."
        )
    )
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--mode", choices=("intersects", "centroid"), default="intersects")
    parser.add_argument("--height-scale", type=float, default=100.0)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--render", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not args.skip_build:
        _build_rust_emit_binary()
    if not RUST_BIN.exists():
        raise SystemExit(f"missing Rust emitter binary: {RUST_BIN}")

    import forge3d
    import forge3d._forge3d as native
    import numpy as np

    engine_info = _engine_info(native)
    print("h3o + Forge3D pipeline benchmark")
    print(f"forge3d_version={getattr(forge3d, '__version__', 'unknown')}")
    print(f"engine={json.dumps(engine_info, separators=(',', ':'), default=str)}")
    print(
        f"mode={args.mode} runs={args.runs} warmups={args.warmups} "
        f"render={args.render} size={args.width}x{args.height}"
    )
    print()

    rows: list[dict[str, Any]] = []
    for case_name in CASES:
        payload = _rust_emit(case_name, args.mode, args.height_scale)
        payload_detail = _payload_detail(payload)

        mesh = native.import_osm_buildings_from_geojson_py(payload, 10.0, "height")
        transforms = _identity_transform(np)

        samples = [
            _measure(
                "rust_h3o_overlap_emit_geojson",
                lambda case_name=case_name: _rust_emit_detail(case_name, args.mode, args.height_scale),
                args,
                collect_before_each=False,
            ),
            _measure(
                "python_parse_bridge_geojson",
                lambda payload=payload: _parse_detail(payload),
                args,
                collect_before_each=False,
            ),
            _measure(
                "forge3d_native_mesh_from_geojson",
                lambda native=native, payload=payload: _forge3d_mesh_detail(native, payload),
                args,
                collect_before_each=False,
            ),
        ]
        if args.render:
            samples.append(
                _measure(
                    "forge3d_render_imported_mesh",
                    lambda native=native, mesh=mesh, transforms=transforms, args=args: _render_detail(
                        native, mesh, transforms, args.width, args.height
                    ),
                    args,
                    collect_before_each=False,
                )
            )
        samples.append(
            _measure(
                "end_to_end_rust_to_forge3d_mesh",
                lambda native=native, case_name=case_name: _end_to_end_detail(
                    native, case_name, args.mode, args.height_scale
                ),
                args,
                collect_before_each=False,
            )
        )
        if args.render:
            samples.append(
                _measure(
                    "end_to_end_rust_to_forge3d_render",
                    lambda native=native, np=np, case_name=case_name, args=args: _end_to_end_render_detail(
                        native,
                        np,
                        case_name,
                        args.mode,
                        args.height_scale,
                        args.width,
                        args.height,
                    ),
                    args,
                    collect_before_each=False,
                )
            )

        metadata = payload_detail["metadata"]
        print(
            f"{case_name} level={metadata.get('admin_level')} res={metadata.get('h3_resolution')} "
            f"features={payload_detail['features']} payload={payload_detail['bytes']}B "
            f"rust_compute_once={metadata.get('rust_compute_ms')}ms"
        )
        for sample in samples:
            row = {
                "case": case_name,
                "path": sample.name,
                "median_ms": round(sample.median_ms, 4),
                "p95_ms": round(sample.p95_ms, 4),
                "detail": sample.detail,
                "engine": engine_info,
            }
            rows.append(row)
            print(
                f"  {sample.name:34s} median={sample.median_ms:9.4f}ms "
                f"p95={sample.p95_ms:9.4f}ms {_detail_summary(sample.detail)}"
            )
        print()

    print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))


def _build_rust_emit_binary() -> None:
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            str(RUST_CRATE / "Cargo.toml"),
            "--bin",
            "emit_overlap_geojson",
        ],
        cwd=ROOT,
        check=True,
    )


def _measure(
    name: str,
    fn: Callable[[], dict[str, Any]],
    args: argparse.Namespace,
    *,
    collect_before_each: bool,
) -> Sample:
    for _ in range(args.warmups):
        fn()

    times_ms: list[float] = []
    detail: dict[str, Any] = {}
    for _ in range(args.runs):
        if collect_before_each:
            gc.collect()
        start = time.perf_counter()
        detail = fn()
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return Sample(name=name, times_ms=times_ms, detail=detail)


def _rust_emit(case_name: str, mode: str, height_scale: float) -> str:
    proc = subprocess.run(
        [
            str(RUST_BIN),
            "--case",
            case_name,
            "--mode",
            mode,
            "--height-scale",
            str(height_scale),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout


def _rust_emit_detail(case_name: str, mode: str, height_scale: float) -> dict[str, Any]:
    payload = _rust_emit(case_name, mode, height_scale)
    detail = _payload_detail(payload)
    metadata = detail["metadata"]
    return {
        "features": detail["features"],
        "bytes": detail["bytes"],
        "rust_compute_ms": metadata.get("rust_compute_ms"),
    }


def _parse_detail(payload: str) -> dict[str, Any]:
    return _payload_detail(payload)


def _payload_detail(payload: str) -> dict[str, Any]:
    parsed = json.loads(payload)
    features = parsed.get("features") or []
    metadata = parsed.get("metadata") or {}
    return {
        "features": len(features),
        "bytes": len(payload.encode("utf-8")),
        "metadata": metadata,
    }


def _forge3d_mesh_detail(native: Any, payload: str) -> dict[str, Any]:
    mesh = native.import_osm_buildings_from_geojson_py(payload, 10.0, "height")
    return _mesh_detail(mesh)


def _mesh_detail(mesh: dict[str, Any]) -> dict[str, Any]:
    return {
        "vertices": int(mesh.get("vertex_count") or len(mesh.get("positions", []))),
        "triangles": int(mesh.get("triangle_count") or len(mesh.get("indices", [])) // 3),
        "mesh_bytes": _mesh_output_bytes(mesh),
    }


def _end_to_end_detail(native: Any, case_name: str, mode: str, height_scale: float) -> dict[str, Any]:
    payload = _rust_emit(case_name, mode, height_scale)
    payload_detail = _payload_detail(payload)
    mesh_detail = _forge3d_mesh_detail(native, payload)
    return {
        "features": payload_detail["features"],
        "bytes": payload_detail["bytes"],
        **mesh_detail,
        "rust_compute_ms": payload_detail["metadata"].get("rust_compute_ms"),
    }


def _end_to_end_render_detail(
    native: Any,
    np: Any,
    case_name: str,
    mode: str,
    height_scale: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    payload = _rust_emit(case_name, mode, height_scale)
    payload_detail = _payload_detail(payload)
    mesh = native.import_osm_buildings_from_geojson_py(payload, 10.0, "height")
    transforms = _identity_transform(np)
    render_detail = _render_detail(native, mesh, transforms, width, height)
    return {
        "features": payload_detail["features"],
        "bytes": payload_detail["bytes"],
        **_mesh_detail(mesh),
        **render_detail,
        "rust_compute_ms": payload_detail["metadata"].get("rust_compute_ms"),
    }


def _render_detail(
    native: Any,
    mesh: dict[str, Any],
    transforms: Any,
    width: int,
    height: int,
) -> dict[str, Any]:
    image = native.geometry_instance_mesh_gpu_render_py(width, height, mesh, transforms)
    shape = tuple(int(value) for value in image.shape)
    alpha_sum = int(image[..., 3].sum()) if len(shape) == 3 and shape[2] == 4 else 0
    return {
        "render_width": width,
        "render_height": height,
        "alpha_sum": alpha_sum,
    }


def _identity_transform(np: Any) -> Any:
    return np.ascontiguousarray(np.eye(4, dtype=np.float32).reshape(1, 16))


def _mesh_output_bytes(mesh: dict[str, Any]) -> int:
    total = 0
    for key in ("positions", "normals", "uvs", "tangents", "indices"):
        value = mesh.get(key)
        if hasattr(value, "nbytes"):
            total += int(value.nbytes)
    return total


def _engine_info(native: Any) -> dict[str, Any]:
    try:
        info = native.engine_info() if hasattr(native, "engine_info") else {}
    except BaseException as exc:
        return {"status": "error", "error": str(exc)}
    if isinstance(info, dict):
        return info
    return {"status": "unknown", "raw": str(info)}


def _detail_summary(detail: dict[str, Any]) -> str:
    parts = []
    for key in (
        "features",
        "vertices",
        "triangles",
        "bytes",
        "mesh_bytes",
        "render_width",
        "render_height",
        "alpha_sum",
        "rust_compute_ms",
    ):
        if key in detail:
            suffix = "B" if key in {"bytes", "mesh_bytes"} else ""
            parts.append(f"{key}={detail[key]}{suffix}")
    return " ".join(parts)


if __name__ == "__main__":
    main()
