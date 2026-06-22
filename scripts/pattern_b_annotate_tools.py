"""Pattern B: append WHEN TO USE / WHEN NOT TO USE disambiguation prose to
the 12 tool descriptions most prone to LLM confusion.

Stolen from OpenClaw's skill-prose pattern — instead of asking the model to
guess which tool fits, name the collision and the resolution rule inline in
the tool description that the model sees at decision time.

Idempotent: looks for the sentinel marker " WHEN TO USE:" and skips tools
that already carry the annotation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS_PATH = Path(__file__).resolve().parents[1] / "src" / "geoprocessing" / "tools.json"

SENTINEL = " WHEN TO USE:"

ANNOTATIONS: dict[str, str] = {
    "get_field_health": (
        " WHEN TO USE: a user-drawn polygon, any geography, optionally a "
        "specific date. WHEN NOT TO USE: Rwanda admin boundary "
        "(district/sector/cell) — use get_agri_indices. Cached weekly "
        "aggregates — use get_ndvi_stats or get_parcel_ndvi_stats."
    ),
    "get_ndvi_stats": (
        " WHEN TO USE: one weekly mean NDVI value for ONE named Rwanda "
        "district. WHEN NOT TO USE: sector/cell granularity — use "
        "get_cell_ndvi_stats. Real-time over a polygon — use "
        "get_field_health. User-uploaded field — use get_parcel_ndvi_stats."
    ),
    "get_cell_ndvi_stats": (
        " WHEN TO USE: sector- or cell-level NDVI within a named Rwanda "
        "district. WHEN NOT TO USE: district-level summary — use "
        "get_ndvi_stats. User-uploaded polygon — use get_parcel_ndvi_stats."
    ),
    "get_parcel_ndvi_stats": (
        " WHEN TO USE: user-uploaded field boundary by slug or name "
        "(e.g. Cyampirita, Kabarama, Kabari). WHEN NOT TO USE: Rwanda admin "
        "boundary — use get_ndvi_stats or get_cell_ndvi_stats. Real-time on "
        "an arbitrary polygon — use get_field_health."
    ),
    "get_agri_indices": (
        " WHEN TO USE: Rwanda ADMIN boundaries (district/sector/cell) "
        "needing live multi-index values now. WHEN NOT TO USE: user-drawn "
        "polygon — use get_field_health. Pre-aggregated weekly NDVI only — "
        "use get_ndvi_stats or get_cell_ndvi_stats."
    ),
    "compute_spectral_index": (
        " WHEN TO USE: the user wants to SEE NDVI/NDWI/NBR as a coloured "
        "layer on the map. WHEN NOT TO USE: numbers/stats only — use "
        "get_field_health or get_ndvi_stats. This tool renders; it does not "
        "return tabular metrics."
    ),
    "identify_parcel_crop": (
        " WHEN TO USE: classify ONE specific parcel right now from Sentinel-2 "
        "time-series. WHEN NOT TO USE: look up an already-classified parcel "
        "— call get_crop_classifications first (cached weekly). General "
        "'what crops grow here' — use get_entity or search_brain."
    ),
    "get_crop_classifications": (
        " WHEN TO USE: read the most recent weekly crop classification result "
        "for parcels from the cache. WHEN NOT TO USE: classify a parcel that "
        "has not been processed yet — use identify_parcel_crop."
    ),
    "get_insurance_intelligence": (
        " WHEN TO USE: open-ended 'what is happening here' / 'how is this "
        "farm doing' over a Rwanda location — fuses NDVI + weather + drought "
        "+ crop into one report. WHEN NOT TO USE: the user asked one narrow "
        "question (just rainfall, just NDVI, just soil) — call the specific "
        "tool. Do NOT default to this for every Rwanda question."
    ),
    "get_drought_status": (
        " WHEN TO USE: drought severity now (VCI + NDWI). WHEN NOT TO USE: "
        "forward-looking yield risk — use get_yield_risk. Recent NDVI drop "
        "alerts — use get_anomaly_alerts. Historical no-rain windows — use "
        "detect_dry_spells."
    ),
    "get_soil_moisture": (
        " WHEN TO USE: WaPOR 100m dekadal (10-day) area-aggregated soil "
        "moisture, optical-derived. WHEN NOT TO USE: point measurement at "
        "sub-daily cadence that sees through clouds and vegetation — use "
        "get_cygnss_soil_moisture. Soil texture/chemistry — use "
        "get_soil_properties."
    ),
    "add_land_cover_layer": (
        " WHEN TO USE: the user wants to SEE land cover on the map. WHEN "
        "NOT TO USE: numeric land-cover area or percentage for a region — "
        "use query_worldcover_stats. This tool only adds a visual layer."
    ),
}


def main() -> int:
    try:
        raw = TOOLS_PATH.read_text()
    except OSError as exc:
        print(f"ERROR: unable to read {TOOLS_PATH}: {exc}", file=sys.stderr)
        return 1

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: unable to parse {TOOLS_PATH}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print("tools.json is not a list", file=sys.stderr)
        return 1

    by_name: dict[str, dict] = {}
    for entry in data:
        fn = entry.get("function") if isinstance(entry, dict) else None
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if name:
            by_name[name] = fn

    annotated = 0
    skipped = 0
    missing: list[str] = []
    for name, prose in ANNOTATIONS.items():
        fn = by_name.get(name)
        if fn is None:
            missing.append(name)
            continue
        desc = fn.get("description", "") or ""
        if SENTINEL in desc:
            skipped += 1
            continue
        fn["description"] = desc.rstrip() + prose
        annotated += 1

    if missing:
        print(f"ERROR: tools not found in tools.json: {missing}", file=sys.stderr)
        return 2

    try:
        TOOLS_PATH.write_text(json.dumps(data, indent=2) + "\n")
    except OSError as exc:
        print(f"ERROR: unable to write {TOOLS_PATH}: {exc}", file=sys.stderr)
        return 1

    print(f"Pattern B: annotated={annotated} skipped={skipped} total_targets={len(ANNOTATIONS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
