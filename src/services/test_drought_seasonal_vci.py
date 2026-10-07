"""Drought is judged against the same time of year in earlier years (2026-10-07).

detect_drought took VCI min/max over whatever history it got (at most a year: the NDVI cache was
purged after 365 days), so the dry season read as drought; and it returned numpy floats, which
psycopg2 wrote as `np.float64(...)`: the 2026-10-05 weekly scan failed with `schema "np" does
not exist` and drought_cache stayed empty. The last test runs the real weekly_drought_scan asset
against the test database (Dagster materialize, throwaway instance).
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List

import numpy as np
import pytest

from src.services.ml_inference import CropClassifier

RUN_TAG = uuid.uuid4().hex[:8]


def _weekly(start: date, end: date, ndvi, ndwi=None) -> List[Dict[str, Any]]:
    """Weekly rows as weekly_drought_scan builds them from ndvi_field_cache."""
    rows, day = [], start
    while day <= end:
        rows.append({"date": str(day), "mean_ndvi": ndvi(day), "mean_ndwi": ndwi(day) if ndwi else None})
        day += timedelta(days=7)
    return rows


def _seasonal(day: date, offset_by_year: Dict[int, float]) -> float:
    """A rainy/dry cycle: highest in April, lowest in October, shifted a little per year."""
    return 0.5 + 0.2 * math.cos(2 * math.pi * (day.timetuple().tm_yday - 105) / 365) + offset_by_year.get(day.year, 0.0)


def _plain(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_plain(v) for v in value.values())
    if isinstance(value, list):
        return all(_plain(v) for v in value)
    return not isinstance(value, np.generic)


def test_one_season_of_history_is_insufficient_not_drought():
    # Bugesera-like: 13 weeks from 2026-07-03 into the dry season, NDVI falling.
    history = _weekly(date(2026, 7, 3), date(2026, 9, 28), lambda d: 0.45 - 0.002 * (d - date(2026, 7, 3)).days)
    result = CropClassifier().detect_drought(history)
    assert result["drought_status"] == "insufficient_data"
    assert result["current_vci"] is None
    assert result["baseline_years"] == 0
    assert "history starts 2026-07-03" in result["description"]


def test_a_normal_dry_season_is_not_drought():
    offsets = {2023: 0.03, 2024: -0.03, 2025: 0.02, 2026: 0.0}
    history = _weekly(date(2023, 10, 2), date(2026, 9, 28), lambda d: _seasonal(d, offsets))
    result = CropClassifier().detect_drought(history)
    assert result["baseline_years"] >= 2
    assert result["drought_status"] in ("normal", "watch")
    assert result["current_vci"] > 35
    # One-year min/max, as before, would put this dry-season week near the minimum: VCI ~0.
    last_year = [r["mean_ndvi"] for r in history[-52:]]
    assert (last_year[-1] - min(last_year)) / (max(last_year) - min(last_year)) * 100 < 10


def test_a_dry_season_far_below_earlier_years_is_drought():
    offsets = {2023: 0.03, 2024: 0.0, 2025: 0.02, 2026: -0.12}
    history = _weekly(date(2023, 10, 2), date(2026, 9, 28), lambda d: _seasonal(d, offsets))
    result = CropClassifier().detect_drought(history)
    assert result["drought_status"] == "severe_drought"
    assert result["current_vci"] < 20


def test_the_result_holds_plain_python_values():
    offsets = {2023: 0.03, 2024: -0.03, 2025: 0.02}
    history = _weekly(date(2023, 10, 2), date(2026, 9, 28), lambda d: _seasonal(d, offsets), lambda d: 0.05)
    result = CropClassifier().detect_drought(history)
    assert _plain(result)
    json.dumps(result)


@pytest.mark.postgres
def test_the_weekly_scan_writes_its_assessment():
    from dagster import materialize

    from src.pipelines.resources import PostgresResource
    from src.pipelines.rwanda_assets import weekly_drought_scan

    postgres = PostgresResource.from_env()
    districts = {f"Seasonal-{RUN_TAG}": {2023: 0.03, 2024: -0.03, 2025: 0.02}, f"New-{RUN_TAG}": {}}
    starts = {f"Seasonal-{RUN_TAG}": date(2023, 10, 2), f"New-{RUN_TAG}": date(2026, 7, 3)}
    with postgres.get_sync_connection() as conn, conn.cursor() as cur:
        for name, offsets in districts.items():
            for row in _weekly(starts[name], date.today(), lambda d, o=offsets: _seasonal(d, o)):
                cur.execute(
                    "INSERT INTO ndvi_field_cache (district, week_start, mean_ndvi) VALUES (%s, %s, %s)",
                    (name, row["date"], row["mean_ndvi"]),
                )
        conn.commit()
    try:
        # A real run, as the schedule does it, on a throwaway in-process instance.
        run = materialize([weekly_drought_scan], resources={"postgres": postgres})
        assert run.success
        assert run.output_for_node("weekly_drought_scan")["status"] == "ok"
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT district, drought_status, current_vci FROM drought_cache WHERE district = ANY(%s)",
                (list(districts),),
            )
            written = {d: (status, vci) for d, status, vci in cur.fetchall()}
        assert written[f"New-{RUN_TAG}"] == ("insufficient_data", None)
        status, vci = written[f"Seasonal-{RUN_TAG}"]
        assert status in ("normal", "watch") and vci > 35
    finally:
        with postgres.get_sync_connection() as conn, conn.cursor() as cur:
            for table in ("ndvi_field_cache", "drought_cache"):
                cur.execute(f"DELETE FROM {table} WHERE district = ANY(%s)", (list(districts),))
            conn.commit()
