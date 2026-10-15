"""Tests for Dagster pipeline definitions and integrations.

Tests cover:
- Job, schedule and resource definitions
- Resource configuration
- Upload handler import
"""

import os
from unittest.mock import patch

import pytest

dagster = pytest.importorskip("dagster", reason="dagster not installed")

from src.pipelines import (
    defs,
    resources,
    rwanda_assets,
)


class TestPipelineDefinitions:
    """Test suite for Dagster pipeline definitions."""

    def test_definitions_structure(self):
        """Test that all pipeline components are properly defined."""
        assert defs is not None

        # Check assets
        assert len(defs.assets) > 0

        job_names = {job.name for job in defs.jobs}
        assert job_names == {
            "rwanda_bootstrap_job",
            "h3_admin_index_job",
            "nightly_field_ndvi_job",
            "weekly_yield_risk_job",
            "weekly_drought_scan_job",
            "weekly_phenology_job",
            "nightly_cache_cleanup_job",
            "nightly_parcel_ndvi_job",
            "daily_weather_ingest_job",
        }

        assert not defs.sensors

        schedule_names = {schedule.name for schedule in defs.schedules}
        assert schedule_names == {
            "nightly_field_ndvi",
            "weekly_yield_risk",
            "weekly_drought_scan",
            "weekly_phenology",
            "nightly_cache_cleanup",
            "nightly_parcel_ndvi",
            "daily_weather_ingest",
        }

        assert set(defs.resources) == {"postgres", "duckdb"}

    def test_every_schedule_targets_a_defined_job(self):
        job_names = {job.name for job in defs.jobs}
        for schedule in defs.schedules:
            assert schedule.job_name in job_names


class TestResources:
    """Test suite for Dagster resources."""

    def test_postgres_resource_from_env(self):
        """Test PostgreSQL resource configuration from environment."""
        with patch.dict(os.environ, {
            "POSTGRES_HOST": "test-db",
            "POSTGRES_PORT": "5433",
            "POSTGRES_DB": "testdb",
            "POSTGRES_USER": "testuser",
            "POSTGRES_PASSWORD": "testpass",
        }):
            pg_resource = resources.PostgresResource.from_env()

            assert pg_resource.host == "test-db"
            assert pg_resource.port == 5433
            assert pg_resource.database == "testdb"
            assert pg_resource.user == "testuser"
            assert pg_resource.password == "testpass"

            # Test connection string
            conn_str = pg_resource.get_connection_string()
            assert "postgresql://testuser:testpass@test-db:5433/testdb" == conn_str

    def test_duckdb_resource(self):
        """Test DuckDB resource configuration."""
        duckdb_resource = resources.DuckDBResource(
            database_path=":memory:",
            read_only=False,
        )

        assert duckdb_resource.database_path == ":memory:"
        assert duckdb_resource.read_only is False


class TestUploadHandlerIntegration:
    """Upload handlers stay importable alongside the pipeline package."""

    def test_raster_handler_exists(self):
        """Test raster handler module can be imported."""
        from src.upload.handlers import raster_handler

        assert hasattr(raster_handler, "RasterUploadHandler")


class TestAssetExecution:
    """Test suite for asset execution logic (smoke tests)."""

    def test_weekly_yield_risk_asset_exists(self):
        """Test that weekly_yield_risk asset is defined."""
        assert hasattr(rwanda_assets, "weekly_yield_risk")
        asset_fn = getattr(rwanda_assets, "weekly_yield_risk")
        assert callable(asset_fn)

    def test_weekly_drought_scan_asset_exists(self):
        """Test that weekly_drought_scan asset is defined."""
        assert hasattr(rwanda_assets, "weekly_drought_scan")
        asset_fn = getattr(rwanda_assets, "weekly_drought_scan")
        assert callable(asset_fn)

    def test_weekly_phenology_asset_exists(self):
        """Test that weekly_phenology asset is defined."""
        assert hasattr(rwanda_assets, "weekly_phenology")
        asset_fn = getattr(rwanda_assets, "weekly_phenology")
        assert callable(asset_fn)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
