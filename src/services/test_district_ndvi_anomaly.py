"""district_ndvi_anomaly: every district's monthly NDVI anomaly, worst first, alerts and missing."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from unittest.mock import AsyncMock

import pytest

from src.services import admin_boundaries, deafrica_stac, district_ndvi_anomaly
from src.services.district_ndvi_anomaly import district_ndvi_anomalies

TODAY = date(2026, 10, 7)
DISTRICTS = ["Bugesera", "Gasabo", "Huye", "Rubavu", "Rutsiro"]


def _anomaly(z, clear=0.95, month="2026-09"):
    """What deafrica_stac.area_ndvi_anomaly returns (its code): z and ndvi are None below the clear minimum."""
    known = z is not None
    return {"month": month, "z": z, "ndvi": 0.4 if known else None, "clear_fraction": clear,
            "source": deafrica_stac.NDVI_ANOMALY_SOURCE}


@pytest.fixture
def districts(monkeypatch):
    """Five districts with boundaries; each test sets what the reader returns per district."""
    monkeypatch.setattr(admin_boundaries, "list_admin_units", AsyncMock(return_value={
        "level": "district", "within": {}, "count": len(DISTRICTS),
        "units": [{"name": n} for n in DISTRICTS], "truncated": False}))

    async def geometry(district=None, **_):
        return {"type": "Point", "coordinates": [30.0, -2.0], "district": district}
    monkeypatch.setattr(admin_boundaries, "lookup_admin_geometry", geometry)

    def use(by_district):
        def read(geometry, not_after):
            assert not_after == TODAY
            value = by_district[geometry["district"]]
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return value()
            return value
        monkeypatch.setattr(deafrica_stac, "area_ndvi_anomaly", read)
    return use


async def test_districts_worst_first_with_alert_classes_and_month(districts):
    districts({"Bugesera": _anomaly(-0.68), "Gasabo": _anomaly(-1.27), "Huye": _anomaly(-0.23),
               "Rubavu": _anomaly(-1.5), "Rutsiro": _anomaly(-1.0)})

    result = await district_ndvi_anomalies(None, TODAY)

    assert [r["district"] for r in result["districts"]] == ["Rubavu", "Gasabo", "Rutsiro", "Bugesera", "Huye"]
    assert [r["alert"] for r in result["districts"]] == ["high", "moderate", "moderate", None, None]
    assert [r["district"] for r in result["alerts"]] == ["Rubavu", "Gasabo", "Rutsiro"]
    assert result["districts"][0]["month"] == "2026-09"
    assert result["districts"][0]["month_ended_days_ago"] == 7
    assert result["source"] == deafrica_stac.NDVI_ANOMALY_SOURCE
    assert "high" in result["scale"] and result["checked"] == 5 and result["missing"] == []


async def test_a_cloudy_or_unpublished_district_is_missing_with_its_reason_never_zero(districts):
    districts({"Bugesera": _anomaly(None, clear=0.12), "Gasabo": None, "Huye": _anomaly(0.0),
               "Rubavu": _anomaly(-0.5), "Rutsiro": _anomaly(-0.4)})

    result = await district_ndvi_anomalies(None, TODAY)

    assert {r["district"] for r in result["districts"]} == {"Huye", "Rubavu", "Rutsiro"}
    assert next(r for r in result["districts"] if r["district"] == "Huye")["z"] == 0.0  # zero is a value
    reasons = {m["district"]: m["reason"] for m in result["missing"]}
    assert reasons["Bugesera"] == "only 12% of it had a clear view in 2026-09 (needs 30%)"
    assert reasons["Gasabo"] == "no month published for it in the last two months"


async def test_a_read_past_the_deadline_is_named_and_the_rest_still_answer(districts):
    def slow():
        time.sleep(2.0)
        return _anomaly(-2.0)
    districts({"Bugesera": slow, "Gasabo": RuntimeError("HTTP 503"), "Huye": _anomaly(-0.2),
               "Rubavu": _anomaly(-0.3), "Rutsiro": _anomaly(-0.4)})

    started = asyncio.get_running_loop().time()
    result = await district_ndvi_anomalies(None, TODAY, deadline_s=0.8)

    assert asyncio.get_running_loop().time() - started < 1.9
    assert [r["district"] for r in result["districts"]] == ["Rutsiro", "Rubavu", "Huye"]
    reasons = {m["district"]: m["reason"] for m in result["missing"]}
    assert reasons["Bugesera"] == "not read within 1 s; ask again shortly"
    assert reasons["Gasabo"] == "the read failed (HTTP 503)"


async def test_one_district_by_any_case_and_a_severity_filter(districts):
    districts({"Gasabo": _anomaly(-1.27), "Rubavu": _anomaly(-1.6)})

    one = await district_ndvi_anomalies(None, TODAY, district="  gasabo ")
    assert one["checked"] == 1 and [r["district"] for r in one["districts"]] == ["Gasabo"]

    high = await district_ndvi_anomalies(None, TODAY, district="Rubavu", severity="high")
    assert [r["district"] for r in high["alerts"]] == ["Rubavu"]
    moderate = await district_ndvi_anomalies(None, TODAY, district="Rubavu", severity="moderate")
    assert moderate["alerts"] == [] and moderate["districts"][0]["alert"] == "high"


async def test_unknown_district_or_severity_is_an_error_not_an_empty_answer(districts):
    districts({})
    with pytest.raises(ValueError, match="no district named 'Kigali'"):
        await district_ndvi_anomalies(None, TODAY, district="Kigali")
    with pytest.raises(ValueError, match="severity must be one of high, moderate"):
        await district_ndvi_anomalies(None, TODAY, severity="severe")


def test_reads_stay_under_the_sage_tool_limit():
    assert district_ndvi_anomaly.DEADLINE_S < 120
