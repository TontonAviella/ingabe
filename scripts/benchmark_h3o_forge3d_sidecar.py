#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
RUST_BIN = RUST_CRATE / "target" / "release" / "mesh_sidecar"


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


class MeshSidecar:
    def __init__(self, np: Any) -> None:
        self._np = np
        self._proc = subprocess.Popen(
            [str(RUST_BIN)],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("mesh sidecar pipes were not created")

    def close(self) -> None:
        if self._proc.poll() is None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(b"quit\n")
                self._proc.stdin.flush()
            except BrokenPipeError:
                pass
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()

    def request(self, case_name: str, mode: str, height_scale: float) -> tuple[dict[str, Any], dict[str, Any]]:
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        command = f"mesh {case_name} {mode} {height_scale}\n".encode("utf-8")
        self._proc.stdin.write(command)
        self._proc.stdin.flush()

        header_line = self._proc.stdout.readline()
        if not header_line:
            stderr = self._proc.stderr.read().decode("utf-8", "replace") if self._proc.stderr else ""
            raise RuntimeError(f"mesh sidecar stopped unexpectedly: {stderr}")
        header = json.loads(header_line)
        if not header.get("ok"):
            raise RuntimeError(str(header.get("error") or header))

        positions_bytes = self._read_exact(int(header["positions_f32"]) * 4)
        normals_bytes = self._read_exact(int(header["normals_f32"]) * 4)
        uvs_bytes = self._read_exact(int(header["uvs_f32"]) * 4)
        indices_bytes = self._read_exact(int(header["indices_u32"]) * 4)
        np = self._np
        mesh = {
            "positions": np.frombuffer(positions_bytes, dtype="<f4").reshape(-1, 3),
            "normals": np.frombuffer(normals_bytes, dtype="<f4").reshape(-1, 3),
            "uvs": np.frombuffer(uvs_bytes, dtype="<f4").reshape(-1, 2),
            "tangents": np.empty((0, 4), dtype=np.float32),
            "indices": np.frombuffer(indices_bytes, dtype="<u4"),
            "vertex_count": int(header["vertices"]),
            "triangle_count": int(header["triangles"]),
        }
        return header, mesh

    def _read_exact(self, size: int) -> bytes:
        assert self._proc.stdout is not None
        chunks = []
        remaining = size
        while remaining:
            chunk = self._proc.stdout.read(remaining)
            if not chunk:
                raise RuntimeError(f"expected {remaining} more bytes from mesh sidecar")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark a persistent h3o+geo Rust sidecar that returns binary mesh "
            "buffers directly to Forge3D, avoiding CLI-per-call and GeoJSON parsing."
        )
    )
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--mode", choices=("intersects", "centroid"), default="intersects")
    parser.add_argument("--height-scale", type=float, default=100.0)
    parser.add_argument("--width", type=int, default=256)
    parser.add_argument("--height", type=int, default=256)
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args()

    if not args.skip_build:
        _build_sidecar()
    if not RUST_BIN.exists():
        raise SystemExit(f"missing Rust sidecar binary: {RUST_BIN}")

    import forge3d
    import forge3d._forge3d as native
    import numpy as np

    engine_info = _engine_info(native)
    transforms = np.ascontiguousarray(np.eye(4, dtype=np.float32).reshape(1, 16))
    sidecar = MeshSidecar(np)
    try:
        print("h3o + Forge3D binary sidecar benchmark")
        print(f"forge3d_version={getattr(forge3d, '__version__', 'unknown')}")
        print(f"engine={json.dumps(engine_info, separators=(',', ':'), default=str)}")
        print(f"mode={args.mode} runs={args.runs} warmups={args.warmups} size={args.width}x{args.height}")
        print()

        rows: list[dict[str, Any]] = []
        for case_name in CASES:
            header, mesh = sidecar.request(case_name, args.mode, args.height_scale)
            samples = [
                _measure(
                    "sidecar_binary_mesh",
                    lambda case_name=case_name: _sidecar_detail(
                        sidecar, case_name, args.mode, args.height_scale
                    ),
                    args,
                ),
                _measure(
                    "forge3d_render_sidecar_mesh",
                    lambda mesh=mesh: _render_detail(native, mesh, transforms, args.width, args.height),
                    args,
                ),
                _measure(
                    "end_to_end_sidecar_to_forge3d_render",
                    lambda case_name=case_name: _end_to_end_detail(
                        sidecar,
                        native,
                        transforms,
                        case_name,
                        args.mode,
                        args.height_scale,
                        args.width,
                        args.height,
                    ),
                    args,
                ),
            ]

            print(
                f"{case_name} features={header['features']} cells={header['cells']} "
                f"vertices={header['vertices']} triangles={header['triangles']} "
                f"mesh={header['mesh_bytes']}B rust_compute_once={header['rust_compute_ms']}ms"
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
                    f"  {sample.name:36s} median={sample.median_ms:9.4f}ms "
                    f"p95={sample.p95_ms:9.4f}ms {_detail_summary(sample.detail)}"
                )
            print()

        print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))
    finally:
        sidecar.close()


def _build_sidecar() -> None:
    subprocess.run(
        [
            "cargo",
            "build",
            "--release",
            "--manifest-path",
            str(RUST_CRATE / "Cargo.toml"),
            "--bin",
            "mesh_sidecar",
        ],
        cwd=ROOT,
        check=True,
    )


def _measure(name: str, fn: Callable[[], dict[str, Any]], args: argparse.Namespace) -> Sample:
    for _ in range(args.warmups):
        fn()

    times_ms: list[float] = []
    detail: dict[str, Any] = {}
    for _ in range(args.runs):
        start = time.perf_counter()
        detail = fn()
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return Sample(name=name, times_ms=times_ms, detail=detail)


def _sidecar_detail(
    sidecar: MeshSidecar,
    case_name: str,
    mode: str,
    height_scale: float,
) -> dict[str, Any]:
    header, _mesh = sidecar.request(case_name, mode, height_scale)
    return _header_detail(header)


def _end_to_end_detail(
    sidecar: MeshSidecar,
    native: Any,
    transforms: Any,
    case_name: str,
    mode: str,
    height_scale: float,
    width: int,
    height: int,
) -> dict[str, Any]:
    header, mesh = sidecar.request(case_name, mode, height_scale)
    render = _render_detail(native, mesh, transforms, width, height)
    return {**_header_detail(header), **render}


def _header_detail(header: dict[str, Any]) -> dict[str, Any]:
    return {
        "features": int(header["features"]),
        "vertices": int(header["vertices"]),
        "triangles": int(header["triangles"]),
        "mesh_bytes": int(header["mesh_bytes"]),
        "rust_compute_ms": header["rust_compute_ms"],
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
        "mesh_bytes",
        "render_width",
        "render_height",
        "alpha_sum",
        "rust_compute_ms",
    ):
        if key in detail:
            suffix = "B" if key == "mesh_bytes" else ""
            parts.append(f"{key}={detail[key]}{suffix}")
    return " ".join(parts)


if __name__ == "__main__":
    main()
