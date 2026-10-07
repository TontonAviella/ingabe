"""area_ndvi_anomaly: the month it picks, the area's own pixels, and the clear-view threshold.

Rasters are written to disk in Digital Earth Africa's grid (EPSG:6933, 30 m) and read with the real
rasterio, so the masking and the CRS handling are the library's, not a mock's.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds

from src.services import deafrica_stac
from src.services.stac_service import stac_datetime_interval

# A 2 x 2 km square near Kigali (WGS84) and a 100 x 100 pixel tile around it.
AREA = {"type": "Polygon", "coordinates": [[[30.05, -1.96], [30.0682, -1.96], [30.0682, -1.9419],
                                            [30.05, -1.9419], [30.05, -1.96]]]}


def _tile(tmp_path, name, values, clear):
    left, bottom, right, top = transform_bounds("EPSG:4326", "EPSG:6933", 30.04, -1.97, 30.08, -1.93)
    transform = from_origin(left, top, 30, 30)
    shape = (int((top - bottom) / 30) + 1, int((right - left) / 30) + 1)
    paths = []
    for band, data, dtype in (("ndvi_std_anomaly", np.full(shape, values[0]), "float32"),
                              ("ndvi_mean", np.full(shape, values[1]), "float32"),
                              ("clear_count", np.full(shape, clear), "int8")):
        path = tmp_path / f"{name}_{band}.tif"
        with rasterio.open(path, "w", driver="GTiff", width=shape[1], height=shape[0], count=1, dtype=dtype,
                           crs="EPSG:6933", transform=transform) as dst:
            dst.write(data.astype(dtype), 1)
        paths.append(str(path))
    return paths


def _item(month, end, paths):
    return {"properties": {"datetime": f"{month}-01T00:00:00Z", "end_datetime": f"{end}T23:59:59.999Z"},
            "assets": {band: {"href": href} for band, href in zip(("ndvi_std_anomaly", "ndvi_mean", "clear_count"), paths)}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    deafrica_stac._area_month_anomaly.cache_clear()


def test_the_latest_month_ended_by_the_date_over_the_areas_clear_pixels(tmp_path, monkeypatch):
    aug = _item("2026-08", "2026-08-31", _tile(tmp_path, "aug", (-0.4, 0.40), 3))
    sep = _item("2026-09", "2026-09-30", _tile(tmp_path, "sep", (-1.2, 0.33), 2))
    oct_ = _item("2026-10", "2026-10-31", _tile(tmp_path, "oct", (-3.0, 0.10), 2))  # not over yet
    searched = {}

    def search(collection, bbox, limit=1, datetime_range=None):
        searched.update(collection=collection, datetime_range=datetime_range)
        return [aug, sep, oct_]

    monkeypatch.setattr(deafrica_stac, "_search_collection_items", search)
    result = deafrica_stac.area_ndvi_anomaly(AREA, date(2026, 10, 7))
    assert searched == {"collection": "ndvi_anomaly",  # the 70 days to the date, whole days
                        "datetime_range": stac_datetime_interval("2026-07-29/2026-10-07")}
    assert result["month"] == "2026-09"
    assert result["z"] == pytest.approx(-1.2)
    assert result["ndvi"] == pytest.approx(0.33, abs=1e-3)
    assert result["clear_fraction"] == pytest.approx(1.0, abs=0.05)  # the square's pixels, not the tile's
    assert result["source"] == deafrica_stac.NDVI_ANOMALY_SOURCE


def test_a_month_with_no_clear_view_of_the_area_is_unknown_not_zero(tmp_path, monkeypatch):
    sep = _item("2026-09", "2026-09-30", _tile(tmp_path, "sep", (-1.2, 0.33), 0))
    monkeypatch.setattr(deafrica_stac, "_search_collection_items", lambda *a, **k: [sep])
    result = deafrica_stac.area_ndvi_anomaly(AREA, date(2026, 10, 7))
    assert result["month"] == "2026-09"
    assert result["z"] is None and result["ndvi"] is None
    assert result["clear_fraction"] == 0.0


def test_no_published_month_is_none(monkeypatch):
    monkeypatch.setattr(deafrica_stac, "_search_collection_items", lambda *a, **k: [])
    assert deafrica_stac.area_ndvi_anomaly(AREA, date(2026, 10, 7)) is None
