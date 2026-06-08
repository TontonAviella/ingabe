#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
import sys
import statistics
import time
import tracemalloc
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.services.forge3d_adapter import build_forge3d_impact_layer, forge3d_available
from src.services.rain_impact import RainImpactInput, analyze_expected_rain_impact


BBOX = [30.0, -2.05, 30.12, -1.93]
RAIN_STYLE = {
    "color_property": "risk_score",
    "stops": [
        {"max": 35, "color": "#2ecc71"},
        {"max": 55, "color": "#f1c40f"},
        {"max": 75, "color": "#e67e22"},
        {"max": 101, "color": "#c0392b"},
    ],
    "fill_opacity": 0.62,
    "stroke_color": "#1f2937",
    "stroke_width": 1.5,
    "extrude_3d": True,
    "extrusion_property": "risk_score",
    "extrusion_scale": 55,
}


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
            "Benchmark Ingabe impact-map preparation paths: current MapLibre/GeoJSON "
            "payloads versus Forge3D BuildingLayer construction."
        )
    )
    parser.add_argument("--features", nargs="+", type=int, default=[16, 256, 1024, 4096])
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    args = parser.parse_args()

    forge_ok, forge_version, forge_error = forge3d_available()
    print("Spatial engine benchmark")
    print(f"forge3d_available={forge_ok} version={forge_version} error={forge_error}")
    print(f"runs={args.runs} warmups={args.warmups}")
    print()

    rows: list[dict[str, Any]] = []
    for feature_count in args.features:
        exposure_geojson = _make_grid_geojson(feature_count)
        rain_result = analyze_expected_rain_impact(_rain_payload(exposure_geojson))
        scored_geojson = rain_result["geojson"]

        samples = [
            _measure(
                "rain_score_existing",
                lambda: analyze_expected_rain_impact(_rain_payload(exposure_geojson)),
                runs=args.runs,
                warmups=args.warmups,
            ),
            _measure(
                "maplibre_payload_existing",
                lambda: _build_maplibre_geojson_payload(scored_geojson),
                runs=args.runs,
                warmups=args.warmups,
            ),
            _measure(
                "forge3d_building_layer",
                lambda: build_forge3d_impact_layer(
                    scored_geojson,
                    height_property=rain_result["map"]["height_property"],
                    height_scale=rain_result["map"]["height_scale"],
                ),
                runs=args.runs,
                warmups=args.warmups,
            ),
        ]

        for sample in samples:
            row = {
                "features": feature_count,
                "path": sample.name,
                "mean_ms": round(sample.mean_ms, 3),
                "median_ms": round(sample.median_ms, 3),
                "p95_ms": round(sample.p95_ms, 3),
                "peak_kib": round(sample.peak_kib, 1),
                "detail": _compact_detail(sample.detail),
            }
            rows.append(row)
            print(
                f"{feature_count:5d} {sample.name:26s} "
                f"median={sample.median_ms:8.2f}ms "
                f"p95={sample.p95_ms:8.2f}ms "
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
    for _ in range(warmups):
        fn()

    times_ms: list[float] = []
    peak_kib = 0.0
    detail: dict[str, Any] = {}
    for _ in range(runs):
        gc.collect()
        tracemalloc.start()
        start = time.perf_counter()
        detail = fn()
        elapsed = (time.perf_counter() - start) * 1000.0
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        times_ms.append(elapsed)
        peak_kib = max(peak_kib, peak / 1024.0)
    return Sample(name=name, times_ms=times_ms, peak_kib=peak_kib, detail=detail)


def _rain_payload(exposure_geojson: str) -> RainImpactInput:
    return RainImpactInput(
        location_label="Benchmark farms",
        bbox=BBOX,
        rainfall_mm_24h=78,
        rainfall_mm_72h=148,
        soil_saturation="wet",
        crop_stage="flowering",
        forecast_summary="Synthetic heavy-rain scenario for benchmark.",
        exposure_geojson=exposure_geojson,
    )


def _build_maplibre_geojson_payload(geojson: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "source_id": "benchmark-rain-impact",
        "geojson": geojson,
        "name": "Expected Rain Impact - Benchmark farms",
        "bounds": BBOX,
        "style_hint": "rain_impact_risk",
        "style": RAIN_STYLE,
    }
    encoded = json.dumps(payload, separators=(",", ":"))
    decoded = json.loads(encoded)

    style = decoded["style"]
    color_property = style.get("color_property")
    stops = style.get("stops") or []
    fill_color_expr: Any = stops[0]["color"] if stops else "#888"
    if color_property and stops:
        expr: list[Any] = ["step", ["get", color_property], stops[0]["color"]]
        for idx in range(len(stops) - 1):
            expr.extend([stops[idx]["max"], stops[idx + 1]["color"]])
        fill_color_expr = expr

    return {
        "payload_bytes": len(encoded.encode("utf-8")),
        "feature_count": len(decoded["geojson"].get("features") or []),
        "layer_type": "fill-extrusion",
        "fill_color_expr": fill_color_expr,
        "height_expr": [
            "*",
            ["coalesce", ["to-number", ["get", style["extrusion_property"]]], 0],
            style["extrusion_scale"],
        ],
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
                        "id": f"farm-{row}-{col}",
                        "crop": "maize",
                        "area_m2": 1800 + ((row + col) % 7) * 120,
                    },
                }
            )
    return json.dumps({"type": "FeatureCollection", "features": features})


def _detail_summary(detail: dict[str, Any]) -> str:
    if detail.get("available") is False:
        return f"available=False error={detail.get('error')}"
    if "summary" in detail:
        summary = detail["summary"]
        return f"feature_count={summary.get('feature_count')}"
    if "payload_bytes" in detail:
        return f"payload={detail['payload_bytes']}B features={detail['feature_count']}"
    if "building_count" in detail:
        return f"buildings={detail['building_count']} active={detail.get('active')}"
    return ""


def _compact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    if detail.get("available") is False:
        return {
            "available": False,
            "active": detail.get("active"),
            "version": detail.get("version"),
            "error": detail.get("error"),
        }
    if "summary" in detail:
        return {
            "status": detail.get("status"),
            "feature_count": detail.get("summary", {}).get("feature_count"),
        }
    if "payload_bytes" in detail:
        return {
            "payload_bytes": detail["payload_bytes"],
            "feature_count": detail["feature_count"],
            "layer_type": detail["layer_type"],
        }
    if "building_count" in detail:
        return {
            "available": detail.get("available"),
            "active": detail.get("active"),
            "version": detail.get("version"),
            "building_count": detail.get("building_count"),
            "layer_type": detail.get("layer_type"),
        }
    return {}


if __name__ == "__main__":
    main()
