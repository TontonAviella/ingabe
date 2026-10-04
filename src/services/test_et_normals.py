"""ET normals: dekad arithmetic, lookup and attaching normals to a WaPOR series."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock

from src.services import et_normals


def test_dekad_of_year():
    assert et_normals.dekad_of_year("2026-01-D1") == 1
    assert et_normals.dekad_of_year("2026-09-D2") == 26
    assert et_normals.dekad_of_year("2026-12-D3") == 36


def test_dekads_between_follows_the_calendar():
    assert et_normals.dekads_between(date(2026, 9, 15), date(2026, 10, 4)) == [26, 27, 28]
    assert et_normals.dekads_between(date(2026, 1, 31), date(2026, 2, 1)) == [3, 4]  # day 31 is in D3


def test_attach_puts_each_dekads_normal_and_leaves_missing_ones_unknown():
    result = {"status": "success", "time_series": [
        {"dekad": "2026-09-D2", "et_mm_per_day": 1.7},
        {"dekad": "2026-09-D3", "et_mm_per_day": None},
        {"dekad": "2026-10-D1", "et_mm_per_day": 2.0},
    ]}
    et_normals.attach(result, {26: 2.34, 27: 2.08})
    assert [e["normal_et_mm_per_day"] for e in result["time_series"]] == [2.34, 2.08, None]


def test_normals_by_cell_groups_rows():
    conn = AsyncMock()
    conn.fetch.return_value = [
        {"h3_index": "88abc", "dekad": 26, "mean_mm_day": 2.3},
        {"h3_index": "88abc", "dekad": 27, "mean_mm_day": 2.1},
    ]
    out = asyncio.run(et_normals.normals_by_cell(conn, ["88abc", "88abc"], [27, 26]))
    assert out == {"88abc": {26: 2.3, 27: 2.1}}
    assert conn.fetch.call_args.args[1:] == (["88abc"], [26, 27])
