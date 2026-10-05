"""AgERA5 requests match CDS version 2_0, and downloads are readable in this image."""

from __future__ import annotations

import zipfile
from datetime import date

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.transform import from_origin

from src.services import weather_service as ws


class _RecordingClient:
    def __init__(self):
        self.requests: list[dict] = []

    def retrieve(self, dataset, request, target):
        self.requests.append(request)
        open(target, "wb").close()  # empty file: reading fails, which is fine here


def test_requests_use_version_2_0_and_no_statistic_for_flux_variables(monkeypatch):
    service = ws.WeatherService()
    client = _RecordingClient()
    monkeypatch.setattr(service, "_get_client", lambda: client)

    service.download_agera5_day(date(2026, 9, 20))

    by_var = {(r["variable"][0], tuple(r.get("statistic", []))) for r in client.requests}
    assert all(r["version"] == ["2_0"] for r in client.requests)
    assert ("precipitation_flux", ()) in by_var
    assert ("solar_radiation_flux", ()) in by_var
    assert ("2m_temperature", ("24_hour_mean",)) in by_var


def test_netcdf_download_is_read_through_gdal(tmp_path):
    tif = tmp_path / "grid.tif"
    data = np.array([[1.5, 2.5, 3.5], [4.5, 5.5, 6.5]], dtype="float32")
    with rasterio.open(
        tif, "w", driver="GTiff", width=3, height=2, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(29.0, -1.0, 0.1, 0.1),
    ) as dst:
        dst.write(data, 1)
    nc = tmp_path / "Precipitation-Flux_test.nc"
    rasterio.shutil.copy(tif, nc, driver="netCDF")
    archive = tmp_path / "download.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(nc, nc.name)

    result = ws.WeatherService()._extract_and_read_netcdf(str(archive), "precipitation")

    assert result is not None
    assert result["values"].shape == (2, 3)
    assert float(np.nanmean(result["values"])) == float(np.mean(data))
    assert np.allclose(sorted(result["lats"]), [-1.15, -1.05])
    assert np.allclose(result["lons"], [29.05, 29.15, 29.25])
