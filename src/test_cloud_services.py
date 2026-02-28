# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Tests for cloud services: Sentinel Hub, openEO, and upgraded STAC.

Tests cover:
- SentinelHubService configuration and credential detection
- OpenEOService initialization and connection handling
- STACService pystac-client integration and fallback
- Rwanda pre-compute asset helper functions
"""

import os
from unittest.mock import patch

import pytest


# ============================================================================
# Sentinel Hub Service Tests
# ============================================================================


class TestSentinelHubService:
    """Tests for sentinel_hub_service.py."""

    def test_get_service_returns_none_without_package(self):
        """If sentinelhub not installed, get_sentinel_hub_service returns None."""
        with patch("src.services.sentinel_hub_service._SH_AVAILABLE", False):
            from src.services.sentinel_hub_service import get_sentinel_hub_service

            # Reset singleton
            import src.services.sentinel_hub_service as shmod

            shmod._sh_service = None
            result = get_sentinel_hub_service()
            assert result is None

    def test_evalscript_ndvi_structure(self):
        """Verify EVALSCRIPT_NDVI contains required setup and evaluatePixel."""
        from src.services.sentinel_hub_service import EVALSCRIPT_NDVI

        assert "//VERSION=3" in EVALSCRIPT_NDVI
        assert "function setup()" in EVALSCRIPT_NDVI
        assert "function evaluatePixel" in EVALSCRIPT_NDVI
        assert "B04" in EVALSCRIPT_NDVI
        assert "B08" in EVALSCRIPT_NDVI
        assert "dataMask" in EVALSCRIPT_NDVI

    def test_evalscript_multi_index_structure(self):
        """Verify EVALSCRIPT_MULTI_INDEX includes NDVI, NDWI, BSI."""
        from src.services.sentinel_hub_service import EVALSCRIPT_MULTI_INDEX

        assert "//VERSION=3" in EVALSCRIPT_MULTI_INDEX
        assert "ndvi" in EVALSCRIPT_MULTI_INDEX
        assert "ndwi" in EVALSCRIPT_MULTI_INDEX
        assert "bsi" in EVALSCRIPT_MULTI_INDEX
        assert "B02" in EVALSCRIPT_MULTI_INDEX
        assert "B03" in EVALSCRIPT_MULTI_INDEX

    def test_is_configured_without_credentials(self):
        """Service should report not configured without env vars."""
        from src.services.sentinel_hub_service import _SH_AVAILABLE

        if not _SH_AVAILABLE:
            pytest.skip("sentinelhub package not installed")

        with patch.dict(os.environ, {"SH_CLIENT_ID": "", "SH_CLIENT_SECRET": ""}, clear=False):
            from src.services.sentinel_hub_service import SentinelHubService

            import src.services.sentinel_hub_service as shmod

            shmod._sh_service = None
            service = SentinelHubService()
            assert service.is_configured() is False

    def test_get_field_stats_unconfigured_returns_error(self):
        """get_field_stats should return error dict when not configured."""
        from src.services.sentinel_hub_service import _SH_AVAILABLE

        if not _SH_AVAILABLE:
            pytest.skip("sentinelhub package not installed")

        with patch.dict(os.environ, {"SH_CLIENT_ID": "", "SH_CLIENT_SECRET": ""}, clear=False):
            from src.services.sentinel_hub_service import SentinelHubService

            import src.services.sentinel_hub_service as shmod

            shmod._sh_service = None
            service = SentinelHubService()
            result = service.get_field_stats(
                geometry={"type": "Polygon", "coordinates": [[[29.0, -2.0], [29.1, -2.0], [29.1, -1.9], [29.0, -1.9], [29.0, -2.0]]]}
            )
            assert "error" in result
            assert "credentials" in result["error"].lower()


# ============================================================================
# openEO Service Tests
# ============================================================================


class TestOpenEOService:
    """Tests for openeo_service.py."""

    def test_get_service_returns_none_without_package(self):
        """If openeo not installed, get_openeo_service returns None."""
        with patch("src.services.openeo_service._OPENEO_AVAILABLE", False):
            from src.services.openeo_service import get_openeo_service

            import src.services.openeo_service as omod

            omod._openeo_service = None
            result = get_openeo_service()
            assert result is None

    def test_cdse_url_constant(self):
        """Verify CDSE openEO URL is correct."""
        from src.services.openeo_service import CDSE_OPENEO_URL

        assert CDSE_OPENEO_URL == "https://openeo.dataspace.copernicus.eu"

    def test_rwanda_bbox_constant(self):
        """Verify Rwanda bounding box has correct structure."""
        from src.services.openeo_service import RWANDA_BBOX

        assert "west" in RWANDA_BBOX
        assert "south" in RWANDA_BBOX
        assert "east" in RWANDA_BBOX
        assert "north" in RWANDA_BBOX
        assert RWANDA_BBOX["west"] < RWANDA_BBOX["east"]
        assert RWANDA_BBOX["south"] < RWANDA_BBOX["north"]

    def test_init_raises_without_package(self):
        """OpenEOService should raise ImportError if openeo not installed."""
        with patch("src.services.openeo_service._OPENEO_AVAILABLE", False):
            from src.services.openeo_service import OpenEOService

            with pytest.raises(ImportError, match="openeo package not installed"):
                OpenEOService()

    def test_connect_raises_without_credentials(self):
        """_connect should raise ValueError without env vars."""
        from src.services.openeo_service import _OPENEO_AVAILABLE

        if not _OPENEO_AVAILABLE:
            pytest.skip("openeo package not installed")

        with patch.dict(os.environ, {"OPENEO_CLIENT_ID": "", "OPENEO_CLIENT_SECRET": ""}, clear=False):
            from src.services.openeo_service import OpenEOService

            service = OpenEOService()
            with pytest.raises(ValueError, match="OPENEO_CLIENT_ID"):
                service._connect()


# ============================================================================
# STAC Service Tests (pystac-client integration)
# ============================================================================


class TestSTACServiceUpgrade:
    """Tests for stac_service.py pystac-client integration."""

    def test_cdse_catalog_available(self):
        """Verify CDSE catalog endpoint is registered."""
        from src.services.stac_service import STAC_CATALOGS

        assert "cdse" in STAC_CATALOGS
        assert "copernicus" in STAC_CATALOGS["cdse"]

    def test_three_catalogs_available(self):
        """Three STAC catalogs should be registered."""
        from src.services.stac_service import STAC_CATALOGS

        assert len(STAC_CATALOGS) == 3
        assert "earth_search" in STAC_CATALOGS
        assert "planetary_computer" in STAC_CATALOGS
        assert "cdse" in STAC_CATALOGS

    def test_sentinel2_collection_per_catalog(self):
        """Each catalog should have a Sentinel-2 collection ID."""
        from src.services.stac_service import SENTINEL2_COLLECTIONS

        assert "earth_search" in SENTINEL2_COLLECTIONS
        assert "planetary_computer" in SENTINEL2_COLLECTIONS
        assert "cdse" in SENTINEL2_COLLECTIONS

    def test_search_imagery_delegates_to_http_without_pystac(self):
        """When pystac-client is unavailable, search should use HTTP."""
        from src.services.stac_service import STACService

        with patch("src.services.stac_service._PYSTAC_CLIENT_AVAILABLE", False):
            service = STACService()
            assert service._pystac_client is None

            with patch.object(service, "_search_http", return_value={"matched": 0, "items": []}) as mock_http:
                result = service.search_imagery(limit=5)
                mock_http.assert_called_once()
                assert result["matched"] == 0

    def test_compute_ndvi_from_item_missing_bands(self):
        """compute_ndvi_from_item should return error if B04/B08 missing."""
        from src.services.stac_service import STACService

        service = STACService()
        result = service.compute_ndvi_from_item({"id": "test", "assets": {"visual": {}}})
        assert "error" in result
        assert "B04" in result["error"] or "B08" in result["error"]



# Note: DuckDB cache table tests were removed — cache tables migrated
# from DuckDB to PostgreSQL (see alembic migrations for schema).


# ============================================================================
# Rwanda Pre-compute Asset Constants Tests
# ============================================================================


class TestRwandaPrecomputeConstants:
    """Test constants used by Dagster pre-compute assets."""

    def test_rwanda_districts_count(self):
        """Rwanda has 30 administrative districts."""
        from src.pipelines.rwanda_assets import RWANDA_DISTRICTS

        assert len(RWANDA_DISTRICTS) == 30

    def test_rwanda_districts_all_strings(self):
        """All district names should be strings."""
        from src.pipelines.rwanda_assets import RWANDA_DISTRICTS

        assert all(isinstance(d, str) for d in RWANDA_DISTRICTS)

    def test_known_districts_present(self):
        """Check key districts are in the list."""
        from src.pipelines.rwanda_assets import RWANDA_DISTRICTS

        for d in ["Gasabo", "Kicukiro", "Musanze", "Huye", "Rubavu"]:
            assert d in RWANDA_DISTRICTS, f"{d} missing from RWANDA_DISTRICTS"


# ============================================================================
# Schedule Tests
# ============================================================================


class TestPrecomputeSchedules:
    """Test Dagster schedule definitions for pre-compute assets."""

    @pytest.fixture(autouse=True)
    def _skip_without_dagster(self):
        pytest.importorskip("dagster", reason="dagster not installed")

    def test_nightly_schedule_cron(self):
        """Nightly NDVI schedule should run at 2 AM UTC."""
        from src.pipelines.schedules import nightly_field_ndvi_schedule

        assert nightly_field_ndvi_schedule.cron_schedule == "0 2 * * *"
        assert nightly_field_ndvi_schedule.execution_timezone == "UTC"

    def test_weekly_classification_schedule_cron(self):
        """Weekly classification schedule should run Sunday 3 AM UTC."""
        from src.pipelines.schedules import weekly_classification_schedule

        assert weekly_classification_schedule.cron_schedule == "0 3 * * 0"

    def test_weekly_anomaly_schedule_cron(self):
        """Weekly anomaly schedule should run Monday 1 AM UTC."""
        from src.pipelines.schedules import weekly_anomaly_schedule

        assert weekly_anomaly_schedule.cron_schedule == "0 1 * * 1"

    def test_weekly_yield_risk_schedule_cron(self):
        """Weekly yield risk schedule should run Monday 2 AM UTC."""
        from src.pipelines.schedules import weekly_yield_risk_schedule

        assert weekly_yield_risk_schedule.cron_schedule == "0 2 * * 1"

    def test_weekly_drought_schedule_cron(self):
        """Weekly drought schedule should run Monday 3 AM UTC."""
        from src.pipelines.schedules import weekly_drought_schedule

        assert weekly_drought_schedule.cron_schedule == "0 3 * * 1"

    def test_weekly_phenology_schedule_cron(self):
        """Weekly phenology schedule should run Monday 4 AM UTC."""
        from src.pipelines.schedules import weekly_phenology_schedule

        assert weekly_phenology_schedule.cron_schedule == "0 4 * * 1"

    def test_all_precompute_schedules_start_running(self):
        """All pre-compute schedules should start running (credentials configured)."""
        from dagster import DefaultScheduleStatus

        from src.pipelines.schedules import (
            nightly_field_ndvi_schedule,
            weekly_anomaly_schedule,
            weekly_classification_schedule,
            weekly_drought_schedule,
            weekly_phenology_schedule,
            weekly_yield_risk_schedule,
        )

        for sched in [
            nightly_field_ndvi_schedule,
            weekly_classification_schedule,
            weekly_anomaly_schedule,
            weekly_yield_risk_schedule,
            weekly_drought_schedule,
            weekly_phenology_schedule,
        ]:
            assert sched.default_status == DefaultScheduleStatus.RUNNING, (
                f"{sched.name} should start RUNNING"
            )
