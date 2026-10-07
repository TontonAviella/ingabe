"""Insurance reports on fixed inputs, for before/after evidence of an engine change.

CODING_STANDARDS.md asks for a before/after dump on fixed inputs whenever
insurance numbers change. This runs compute_insurance_intelligence with every
remote read replaced by a fixed value (the same seams the engine's tests
patch) and prints the numbers a reader sees: the signals, each trigger, the
status, the confidence and the farmer and insurer texts. Run it on the code
before and after a change and compare.

Fixed inputs: maize, Season A 2026 (planted 2026-09-15), report date
2026-10-07, 3 mm of CHIRPS rain every day, no WaPOR, no forecast, the real
maize Season A trigger rows (insurance_triggers, 2026-10-07). Per scenario: the
NDVI z-score in anomaly_alerts_cache (None = empty) and, for "deafrica", the
area's Digital Earth Africa NDVI anomaly answer.

    PYTHONPATH=. python scripts/insurance_report_dump.py [sar|deafrica] > after.json

Scenario sets: "sar" (default) for the removal of the Sentinel-1 stand-in (#147), "deafrica" for the
switch to Digital Earth Africa's monthly NDVI anomaly; each gives the remote reads fixed answers.

The two docs/evidence/insurance_ndvi_z_*before_after.json files were made by earlier versions of this
script that also fed the engine a radar-predicted NDVI per cell. That predictor
(src/services/sar_ndvi.py) has since been deleted; to rerun them, use the parent of the commit that
deleted it (git log --diff-filter=D -- src/services/sar_ndvi.py).
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import ExitStack
from datetime import date
from unittest.mock import AsyncMock, patch

REF_DATE = date(2026, 10, 7)
RAIN_MM_PER_DAY = 3.0
MAIZE_A_TRIGGERS = [  # insurance_triggers WHERE crop='maize' AND season='A' AND district IS NULL, 2026-10-07
    {"phase": "flowering", "signal": "dry_spell_days", "direction": "above", "threshold": 10, "weight": 0.9,
     "description": "Dry spell during flowering exceeds 10 days"},
    {"phase": "flowering", "signal": "rainfall_cumulative", "direction": "below", "threshold": 40, "weight": 1,
     "description": "Flowering phase rainfall below 40mm critical minimum"},
    {"phase": "full_season", "signal": "dry_spell_days", "direction": "above", "threshold": 15, "weight": 0.6,
     "description": "Maximum dry spell exceeds 15 days"},
    {"phase": "full_season", "signal": "et_anomaly", "direction": "below", "threshold": -20, "weight": 0.4,
     "description": "ET anomaly exceeds -20% deficit"},
    {"phase": "full_season", "signal": "ndvi_z_score", "direction": "below", "threshold": -1.5, "weight": 0.8,
     "description": "NDVI anomaly indicates severe vegetation stress"},
    {"phase": "full_season", "signal": "rainfall_cumulative", "direction": "below", "threshold": 100, "weight": 1,
     "description": "Season cumulative rainfall below 100mm"},
    {"phase": "full_season", "signal": "sar_backscatter", "direction": "below", "threshold": 0.15, "weight": 0.7,
     "description": "SAR VH/VV below 0.15 — low vegetation density"},
    {"phase": "full_season", "signal": "spi", "direction": "below", "threshold": -1, "weight": 0.8,
     "description": "SPI indicates moderate drought"},
]

# (label, cell, district, centre lon, centre lat, NDVI z in anomaly_alerts_cache)
# and, for "deafrica", the area's Digital Earth Africa NDVI anomaly answer.
SAR_SCENARIOS: list[tuple[str, str, str, float, float, float | None]] = [
    ("Kanyangese", "Kanyangese", "Gatsibo", 30.42769, -1.70407, None),
    ("Nyarubuye", "Nyarubuye", "Kamonyi", 29.96406, -2.05292, None),
    ("Kamate", "Kamate", "Nyagatare", 30.45351, -1.37083, None),
    ("Kayumba", "Kayumba", "Bugesera", 30.07456, -2.13123, None),
    ("Kabarore", "Kabarore", "Gatsibo", 30.37629, -1.60169, None),
    ("Bukomeye", "Bukomeye", "Huye", 29.72651, -2.66045, None),
    ("Gisesero", "Gisesero", "Musanze", 29.55721, -1.55604, None),
    ("Pera", "Pera", "Rusizi", 29.02664, -2.69595, None),
    ("Butunzi", "Butunzi", "Rulindo", 29.95352, -1.66949, None),
    ("Mujuga", "Mujuga", "Nyamagabe", 29.46736, -2.52978, None),
    ("Kayumba, optical anomaly -0.5 in the cache", "Kayumba", "Bugesera", 30.07456, -2.13123, -0.5),
    ("Kayumba, optical anomaly -2.0 in the cache", "Kayumba", "Bugesera", 30.07456, -2.13123, -2.0),
]


def _dea(month: str, z: float | None, ndvi: float | None, clear: float) -> dict:
    return {"month": month, "z": z, "ndvi": ndvi, "clear_fraction": clear,
            "source": "Digital Earth Africa NDVI anomaly (Landsat + Sentinel-2 vs 1984-2020)"}


# The cells' September 2026 answers were read live on 2026-10-07 (area_ndvi_anomaly); the others are
# set to show each rule: a trigger-level anomaly, a cloudy month, a month mostly before planting, and an
# anomaly cache that held an alert (which the old engine averaged and the new one no longer reads).
DEAFRICA_SCENARIOS = [
    ("Kayumba, Sep -1.16", "Kayumba", "Bugesera", 30.07456, -2.13123, None, _dea("2026-09", -1.16, 0.326, 1.0)),
    ("Kanyangese, Sep -0.79", "Kanyangese", "Gatsibo", 30.42769, -1.70407, None, _dea("2026-09", -0.79, 0.371, 0.99)),
    ("Kamate, Sep -1.8 (set)", "Kamate", "Nyagatare", 30.45351, -1.37083, None, _dea("2026-09", -1.8, 0.25, 0.95)),
    ("Kamate, Sep too cloudy (set)", "Kamate", "Nyagatare", 30.45351, -1.37083, None, _dea("2026-09", None, None, 0.12)),
    ("Kamate, latest month Aug: before planting (set)", "Kamate", "Nyagatare", 30.45351, -1.37083, None,
     _dea("2026-08", -1.8, 0.25, 0.95)),
    ("Kayumba, cache alert -2.4, Sep -1.16", "Kayumba", "Bugesera", 30.07456, -2.13123, -2.4,
     _dea("2026-09", -1.16, 0.326, 1.0)),
]


def _patches(stack: ExitStack, lon: float, lat: float, dea: dict | None) -> None:
    def chirps(_lat, _lon, dates, timeout_s=None):
        return {d: RAIN_MM_PER_DAY for d in dates}, set()

    for target, kwargs in (
        ("src.services.admin_boundaries.lookup_admin_geometry",
         {"new_callable": AsyncMock, "return_value": {"type": "Point", "coordinates": [lon, lat]}}),
        ("src.services.insurance_engine.compute_insurance_accuracy_safe", {"new_callable": AsyncMock, "return_value": None}),
        ("src.services.weather_accuracy.detect_dry_spells", {"new_callable": AsyncMock, "return_value": None}),
        ("src.services.weather_accuracy.compute_ndvi_concordance", {"new_callable": AsyncMock, "return_value": None}),
        ("src.services.forecast_fusion.fetch_chirps_daily", {"side_effect": chirps}),
        ("src.services.wapor_service.query_et", {"return_value": None}),
        ("src.services.wapor_service.query_soil_moisture", {"return_value": None}),
        ("src.services.forecast_openmeteo.fetch_openmeteo_multimodel", {"return_value": None}),
        # create: code from before the switch has no such reader
        ("src.services.deafrica_stac.area_ndvi_anomaly", {"return_value": dea, "create": True}),
    ):
        stack.enter_context(patch(target, **kwargs))


async def _report(cell: str, district: str, lon: float, lat: float,
                  cached_z: float | None, dea: dict | None = None) -> dict:
    from src.services.insurance_engine import compute_insurance_intelligence

    async def fetch(sql, *args):  # the trigger rows; no other table has rows (no ET normals)
        return MAIZE_A_TRIGGERS if "insurance_triggers" in sql else []

    conn = AsyncMock()
    conn.fetch.side_effect = fetch
    conn.fetchrow.return_value = {"mean_z": cached_z}
    with ExitStack() as stack:
        _patches(stack, lon, lat, dea)
        farmer = await compute_insurance_intelligence(
            conn, crop="maize", season="A", district=district, cell=cell, audience="farmer", ref_date=REF_DATE)
        insurer = await compute_insurance_intelligence(
            conn, crop="maize", season="A", district=district, cell=cell, audience="insurance", ref_date=REF_DATE)
    d = farmer["data"]
    return {
        "inputs": {"anomaly_cache_ndvi_z": cached_z, "deafrica_anomaly": dea},
        "ndvi_z_score": d["ndvi_z_score"],
        "ndvi_month": d.get("ndvi_month"),
        "sources": d["sources"],
        "triggers": [f"{t['signal']}/{t['phase']}: {t['current_value']} vs {t['threshold']} "
                     f"{'TRIGGERED' if t['triggered'] else 'pass'}" for t in d["triggers"]],
        "triggers_activated": d["triggers_activated"],
        "triggers_total": d["triggers_total"],
        "confidence_score": d["confidence_score"],
        "overall_status": d["overall_status"],
        "recommendation": d["recommendation"],
        "farmer_report": farmer["report"].splitlines(),
        "insurer_report": insurer["report"].splitlines(),
    }


async def _all(which: str) -> dict:
    if which == "deafrica":
        return {label: await _report(cell, district, lon, lat, z, dea)
                for label, cell, district, lon, lat, z, dea in DEAFRICA_SCENARIOS}
    return {label: await _report(cell, district, lon, lat, z)
            for label, cell, district, lon, lat, z in SAR_SCENARIOS}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_all(sys.argv[1] if len(sys.argv) > 1 else "sar")), indent=1, default=str))
