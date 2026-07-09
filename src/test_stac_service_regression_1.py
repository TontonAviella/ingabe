"""Regression coverage for current Copernicus Data Space STAC discovery."""

from src.services.stac_service import SENTINEL2_COLLECTIONS, STAC_CATALOGS


# Regression: ISSUE-001 - CDSE searches used its retired endpoint and collection ID.
# Found by /qa on 2026-07-09
# Report: .gstack/qa-reports/qa-report-localhost-2026-07-09.md
def test_cdse_uses_current_stac_endpoint_and_sentinel_collection() -> None:
    assert STAC_CATALOGS["cdse"] == "https://stac.dataspace.copernicus.eu/v1"
    assert SENTINEL2_COLLECTIONS["cdse"] == "sentinel-2-l2a"
