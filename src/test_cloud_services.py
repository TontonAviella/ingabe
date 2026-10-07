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

"""Tests for cloud services: upgraded STAC.

Tests cover:
- STACService pystac-client integration and fallback
- Rwanda pre-compute asset helper functions
"""

from unittest.mock import patch

import pytest


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
            weekly_drought_schedule,
            weekly_phenology_schedule,
            weekly_yield_risk_schedule,
        )

        for sched in [
            nightly_field_ndvi_schedule,
            weekly_anomaly_schedule,
            weekly_yield_risk_schedule,
            weekly_drought_schedule,
            weekly_phenology_schedule,
        ]:
            assert sched.default_status == DefaultScheduleStatus.RUNNING, (
                f"{sched.name} should start RUNNING"
            )
