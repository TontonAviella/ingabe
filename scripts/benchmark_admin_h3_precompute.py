#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gc
import json
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

from scripts.benchmark_admin_h3 import CASES, Case, _make_admin_geojson
from src.services.admin_h3 import AdminH3Options, admin_geojson_to_h3


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
            "Benchmark precomputing admin-boundary H3 overlap once, then joining "
            "live rain/raster values against the cached crosswalk."
        )
    )
    parser.add_argument("--runs", type=int, default=25)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--join-repeats", type=int, default=1_000)
    parser.add_argument("--max-hexes", type=int, default=50_000)
    args = parser.parse_args()

    print("Admin H3 precompute benchmark")
    print(f"runs={args.runs} warmups={args.warmups} join_repeats={args.join_repeats}")
    print()

    rows: list[dict[str, Any]] = []
    for case in CASES:
        geojson = _make_admin_geojson(case)
        precomputed = _precompute(geojson, case, args.max_hexes)
        records = _records_from_feature_collection(precomputed)
        rain_values = _synthetic_h3_values(records)
        payload = _json_dumps(precomputed)

        samples = [
            _measure(
                "precompute_exact_overlap",
                lambda geojson=geojson, case=case: _precompute_detail(
                    geojson, case, args.max_hexes
                ),
                args.runs,
                args.warmups,
                collect_before_each=True,
            ),
            _measure(
                "load_cached_crosswalk_json",
                lambda payload=payload: _load_payload_detail(payload),
                args.runs,
                args.warmups,
            ),
            _measure(
                "runtime_join_once",
                lambda records=records, rain_values=rain_values: _join_detail(
                    records, rain_values, repeats=1
                ),
                args.runs,
                args.warmups,
            ),
            _measure(
                "runtime_join_1000x",
                lambda records=records, rain_values=rain_values, args=args: _join_detail(
                    records, rain_values, repeats=args.join_repeats
                ),
                args.runs,
                args.warmups,
            ),
        ]

        print(f"{case.name} level={case.admin_level} res={case.resolution}")
        for sample in samples:
            row = {
                "case": case.name,
                "admin_level": case.admin_level,
                "resolution": case.resolution,
                "path": sample.name,
                "median_ms": round(sample.median_ms, 4),
                "p95_ms": round(sample.p95_ms, 4),
                "detail": sample.detail,
            }
            rows.append(row)
            print(
                f"  {sample.name:28s} median={sample.median_ms:9.4f}ms "
                f"p95={sample.p95_ms:9.4f}ms {_detail_summary(sample.detail)}"
            )
        print()

    print("json_summary=" + json.dumps(rows, separators=(",", ":"), default=str))


def _measure(
    name: str,
    fn: Callable[[], dict[str, Any]],
    runs: int,
    warmups: int,
    *,
    collect_before_each: bool = False,
) -> Sample:
    for _ in range(warmups):
        fn()

    times_ms: list[float] = []
    detail: dict[str, Any] = {}
    for _ in range(runs):
        if collect_before_each:
            gc.collect()
        start = time.perf_counter()
        detail = fn()
        times_ms.append((time.perf_counter() - start) * 1000.0)
    return Sample(name=name, times_ms=times_ms, detail=detail)


def _precompute(geojson: dict[str, Any], case: Case, max_hexes: int) -> dict[str, Any]:
    return admin_geojson_to_h3(
        geojson,
        options=AdminH3Options(
            resolution=case.resolution,
            admin_level=case.admin_level,
            id_property=f"{case.admin_level}_id",
            name_property=f"{case.admin_level}_name",
            max_hexes=max_hexes,
            include_geometry=False,
        ),
    )


def _precompute_detail(geojson: dict[str, Any], case: Case, max_hexes: int) -> dict[str, Any]:
    result = _precompute(geojson, case, max_hexes)
    records = _records_from_feature_collection(result)
    payload_bytes = len(_json_dumps(result).encode("utf-8"))
    return {
        "hexes": len(records),
        "bytes": payload_bytes,
    }


def _records_from_feature_collection(feature_collection: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for feature in feature_collection.get("features") or []:
        props = feature.get("properties") or {}
        records.append(
            {
                "h3_index": str(props["h3_index"]),
                "overlap_ratio": float(props.get("overlap_ratio") or 0.0),
                "admin_overlap_ratio": float(props.get("admin_overlap_ratio") or 0.0),
            }
        )
    return records


def _synthetic_h3_values(records: list[dict[str, Any]]) -> dict[str, float]:
    values = {}
    for idx, record in enumerate(records):
        h3_index = record["h3_index"]
        values[h3_index] = 20.0 + float((idx * 37 + len(h3_index)) % 90)
    return values


def _load_payload_detail(payload: str) -> dict[str, Any]:
    loaded = json.loads(payload)
    records = _records_from_feature_collection(loaded)
    return {"hexes": len(records), "bytes": len(payload.encode("utf-8"))}


def _join_detail(
    records: list[dict[str, Any]],
    rain_values: dict[str, float],
    *,
    repeats: int,
) -> dict[str, Any]:
    weighted_total = 0.0
    overlap_total = 0.0
    for _ in range(repeats):
        for record in records:
            overlap = record["overlap_ratio"]
            weighted_total += rain_values.get(record["h3_index"], 0.0) * overlap
            overlap_total += overlap
    average = weighted_total / overlap_total if overlap_total else 0.0
    return {
        "hexes": len(records),
        "repeats": repeats,
        "weighted_average": round(average, 6),
    }


def _json_dumps(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"))


def _detail_summary(detail: dict[str, Any]) -> str:
    parts = []
    if "hexes" in detail:
        parts.append(f"hexes={detail['hexes']}")
    if "repeats" in detail:
        parts.append(f"repeats={detail['repeats']}")
    if "bytes" in detail:
        parts.append(f"bytes={detail['bytes']}B")
    if "weighted_average" in detail:
        parts.append(f"avg={detail['weighted_average']}")
    return " ".join(parts)


if __name__ == "__main__":
    main()
