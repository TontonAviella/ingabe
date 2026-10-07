"""Does radar beat the last clear Sentinel-2 NDVI? (docs/SAR_NDVI_SKILL.md)

Reads the clean-label, pooled radar model's leave-one-place-out predictions
saved by scripts/sar_ndvi_skill.py (docs/evidence/sar_ndvi_skill.json:
cloud-masked Sentinel-2 NDVI of each clear date, 10 places, Apr-Oct 2026) and
compares, for every clear date after a place's first, the radar prediction with
simply repeating that place's previous clear-date NDVI, by the days since it.

    python scripts/sar_ndvi_vs_last_clear.py > docs/evidence/sar_ndvi_vs_last_clear.json
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from statistics import mean

EVIDENCE = Path(__file__).resolve().parent.parent / "docs" / "evidence" / "sar_ndvi_skill.json"
GAP_BANDS = ((0, 20), (20, 40), (40, 10_000))  # days since the previous clear date


def _pairs(rows: list[dict]) -> list[dict]:
    by_place: dict[str, list[dict]] = {}
    for r in rows:
        by_place.setdefault(r["place"], []).append(r)
    out = []
    for place, rs in sorted(by_place.items()):
        rs.sort(key=lambda r: r["date"])
        for prev, cur in zip(rs, rs[1:]):
            out.append({
                "place": place,
                "date": cur["date"],
                "gap_days": (date.fromisoformat(cur["date"]) - date.fromisoformat(prev["date"])).days,
                "last_clear_error": round(abs(prev["truth"] - cur["truth"]), 4),
                "radar_error": round(abs(cur["pred"] - cur["truth"]), 4),
            })
    return out


def _summary(pairs: list[dict]) -> dict:
    if not pairs:
        return {"n": 0}
    return {
        "n": len(pairs),
        "last_clear_mae": round(mean(p["last_clear_error"] for p in pairs), 3),
        "radar_mae": round(mean(p["radar_error"] for p in pairs), 3),
        "radar_closer": sum(p["radar_error"] < p["last_clear_error"] for p in pairs),
    }


def main() -> dict:
    rows = json.loads(EVIDENCE.read_text())["clean_label_models"]["leave_one_place_out"]
    pairs = _pairs(rows)
    gaps = sorted(p["gap_days"] for p in pairs)
    return {
        "source": "docs/evidence/sar_ndvi_skill.json clean_label_models.leave_one_place_out",
        "all": _summary(pairs),
        "median_gap_days": gaps[len(gaps) // 2],
        "by_gap_days": {f"{lo}-{hi if hi < 10_000 else ''}": _summary([p for p in pairs if lo <= p["gap_days"] < hi])
                        for lo, hi in GAP_BANDS},
        "pairs": pairs,
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=1))
