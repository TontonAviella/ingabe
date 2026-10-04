"""WaPOR ET normals: the same dekad in earlier years at the same pixel."""

from __future__ import annotations

from unittest.mock import patch

from src.services import wapor_service as ws


def test_normal_is_the_mean_of_the_same_dekad_in_earlier_years():
    archive = {f"{y}-09-D2": 1.0 + (y - 2018) * 0.1 for y in range(2018, 2026)}
    with patch.object(ws, "_et_point_value", side_effect=lambda dk, lat, lon: archive.get(dk)):
        normals = ws.et_normals(-1.6, 30.0, ["2026-09-D2"])
    assert normals["2026-09-D2"] == sum(archive.values()) / len(archive)  # 2018-2025, not 2026 itself


def test_too_few_years_gives_no_normal():
    with patch.object(ws, "_et_point_value", side_effect=lambda dk, lat, lon: 2.0 if dk.startswith("2018") else None):
        assert ws.et_normals(-1.6, 30.0, ["2026-09-D2"]) == {"2026-09-D2": None}


def test_query_et_attaches_normals_per_dekad():
    with patch.object(ws, "_read_point", return_value=1.8), \
         patch.object(ws, "et_normals", return_value={"2026-09-D2": 1.6}):
        from datetime import date
        out = ws.query_et(-1.6, 30.0, date(2026, 9, 15), date(2026, 9, 18), include_normals=True)
    assert out["time_series"] == [{"dekad": "2026-09-D2", "et_mm_per_day": 1.8, "normal_et_mm_per_day": 1.6}]
