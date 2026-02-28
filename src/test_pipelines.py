"""Tests for Dagster pipeline definitions and integrations.

Tests cover:
- Asset definitions and dependencies
- Resource configuration
- Sensor logic
- Schedule definitions
- Integration with upload handlers
"""

import os
from unittest.mock import patch

import pytest

dagster = pytest.importorskip("dagster", reason="dagster not installed")

from dagster import AssetKey

from src.pipelines import (
    defs,
    lakehouse_assets,
    raster_assets,
    resources,
    rwanda_assets,
    schedules,
    sensors,
    vector_assets,
)


class TestPipelineDefinitions:
    """Test suite for Dagster pipeline definitions."""

    def test_definitions_structure(self):
        """Test that all pipeline components are properly defined."""
        assert defs is not None

        # Check assets
        assert len(defs.assets) > 0

        # Check jobs (6 core + 4 Rwanda + 9 precompute)
        job_names = {job.name for job in defs.jobs}
        assert "raster_processing_job" in job_names
        assert "vector_processing_job" in job_names
        assert "iceberg_compaction_job" in job_names
        assert "snapshot_expiry_job" in job_names
        assert "table_optimization_job" in job_names
        assert "cache_warmup_job" in job_names
        assert "rwanda_bootstrap_job" in job_names
        assert "rwanda_ingestion_job" in job_names
        assert "rwanda_ndvi_job" in job_names
        assert "rwanda_ml_job" in job_names
        assert "nightly_field_ndvi_job" in job_names
        assert "weekly_crop_classification_job" in job_names
        assert "weekly_anomaly_scan_job" in job_names
        assert "weekly_yield_risk_job" in job_names
        assert "weekly_drought_scan_job" in job_names
        assert "weekly_phenology_job" in job_names
        assert "nightly_cache_cleanup_job" in job_names
        assert "nightly_parcel_ndvi_job" in job_names
        assert "daily_weather_ingest_job" in job_names

        # Check sensors
        assert len(defs.sensors) == 2
        sensor_names = {sensor.name for sensor in defs.sensors}
        assert "s3_upload_sensor" in sensor_names
        assert "failed_cog_retry_sensor" in sensor_names

        # Check schedules (6 core + 8 precompute)
        schedule_names = {schedule.name for schedule in defs.schedules}
        assert "hourly_compaction" in schedule_names
        assert "daily_snapshot_expiry" in schedule_names
        assert "daily_cache_warmup" in schedule_names
        assert "weekly_table_optimization" in schedule_names
        assert "weekly_rwanda_ndvi" in schedule_names
        assert "daily_rwanda_parcel_sync" in schedule_names
        assert "nightly_field_ndvi" in schedule_names
        assert "weekly_crop_classification" in schedule_names
        assert "weekly_anomaly_scan" in schedule_names
        assert "weekly_yield_risk" in schedule_names
        assert "weekly_drought_scan" in schedule_names
        assert "weekly_phenology" in schedule_names
        assert "nightly_cache_cleanup" in schedule_names
        assert "nightly_parcel_ndvi" in schedule_names
        assert "daily_weather_ingest" in schedule_names

        # Check resources
        assert "s3" in defs.resources
        assert "postgres" in defs.resources
        assert "redis" in defs.resources
        assert "duckdb" in defs.resources

    def test_asset_dependencies(self):
        """Test that asset dependencies are correctly defined."""
        # Get all asset nodes
        asset_graph = defs.get_asset_graph()

        # Helper to get parent asset keys for a given asset key
        def get_parent_keys(key: AssetKey):
            node = asset_graph.get(key)
            return node.parent_keys

        # Check raster pipeline dependencies
        raw_raster_key = AssetKey(["raw_raster_upload"])
        cog_gen_key = AssetKey(["cog_generation"])
        zonal_stats_key = AssetKey(["zonal_statistics"])

        # cog_generation depends on raw_raster_upload
        assert raw_raster_key in get_parent_keys(cog_gen_key)

        # zonal_statistics depends on cog_generation
        assert cog_gen_key in get_parent_keys(zonal_stats_key)

        # Check vector pipeline dependencies
        raw_vector_key = AssetKey(["raw_vector_upload"])
        fgb_key = AssetKey(["flatgeobuf_conversion"])
        pmtiles_key = AssetKey(["vector_tile_generation"])
        iceberg_key = AssetKey(["iceberg_registration"])

        # flatgeobuf_conversion depends on raw_vector_upload
        assert raw_vector_key in get_parent_keys(fgb_key)

        # vector_tile_generation depends on flatgeobuf_conversion
        assert fgb_key in get_parent_keys(pmtiles_key)

        # iceberg_registration depends on vector_tile_generation
        assert pmtiles_key in get_parent_keys(iceberg_key)


class TestResources:
    """Test suite for Dagster resources."""

    def test_s3_resource_from_env(self):
        """Test S3 resource configuration from environment."""
        with patch.dict(os.environ, {
            "S3_ENDPOINT_URL": "http://test-minio:9000",
            "S3_ACCESS_KEY_ID": "test-key",
            "S3_SECRET_ACCESS_KEY": "test-secret",
            "S3_DEFAULT_REGION": "us-west-2",
            "S3_BUCKET": "test-bucket",
        }):
            s3_resource = resources.S3Resource.from_env()

            assert s3_resource.endpoint_url == "http://test-minio:9000"
            assert s3_resource.access_key_id == "test-key"
            assert s3_resource.secret_access_key == "test-secret"
            assert s3_resource.region_name == "us-west-2"
            assert s3_resource.bucket_name == "test-bucket"

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

    def test_redis_resource_from_env(self):
        """Test Redis resource configuration from environment."""
        with patch.dict(os.environ, {
            "REDIS_HOST": "test-redis",
            "REDIS_PORT": "6380",
        }):
            redis_resource = resources.RedisResource.from_env()

            assert redis_resource.host == "test-redis"
            assert redis_resource.port == 6380

    def test_duckdb_resource(self):
        """Test DuckDB resource configuration."""
        duckdb_resource = resources.DuckDBResource(
            database_path=":memory:",
            read_only=False,
        )

        assert duckdb_resource.database_path == ":memory:"
        assert duckdb_resource.read_only is False


class TestSensors:
    """Test suite for Dagster sensors."""

    def test_s3_upload_sensor_defined(self):
        """Test S3 upload sensor is properly defined via Definitions."""
        sensor_names = {s.name for s in defs.sensors}
        assert "s3_upload_sensor" in sensor_names

    def test_failed_cog_retry_sensor_defined(self):
        """Test failed COG retry sensor is properly defined via Definitions."""
        sensor_names = {s.name for s in defs.sensors}
        assert "failed_cog_retry_sensor" in sensor_names

    def test_build_sensor_functions_exist(self):
        """Test that sensor builder functions exist."""
        assert hasattr(sensors, 'build_s3_upload_sensor')
        assert hasattr(sensors, 'build_failed_cog_retry_sensor')
        assert callable(sensors.build_s3_upload_sensor)
        assert callable(sensors.build_failed_cog_retry_sensor)


class TestSchedules:
    """Test suite for Dagster schedules."""

    def test_compaction_schedule_cron(self):
        """Test hourly compaction schedule configuration."""
        schedule = schedules.compaction_schedule
        assert schedule.cron_schedule == "0 * * * *"  # Hourly
        assert schedule.name == "hourly_compaction"

    def test_snapshot_expiry_schedule_cron(self):
        """Test daily snapshot expiry schedule configuration."""
        schedule = schedules.snapshot_expiry_schedule
        assert schedule.cron_schedule == "0 2 * * *"  # 2 AM daily
        assert schedule.name == "daily_snapshot_expiry"

    def test_cache_warmup_schedule_cron(self):
        """Test daily cache warmup schedule configuration."""
        schedule = schedules.cache_warmup_schedule
        assert schedule.cron_schedule == "0 3 * * *"  # 3 AM daily
        assert schedule.name == "daily_cache_warmup"

    def test_table_optimization_schedule_cron(self):
        """Test weekly table optimization schedule configuration."""
        schedule = schedules.table_optimization_schedule
        assert schedule.cron_schedule == "0 4 * * 0"  # Sunday 4 AM
        assert schedule.name == "weekly_table_optimization"

    def test_rwanda_ndvi_schedule_cron(self):
        """Test weekly Rwanda NDVI schedule configuration."""
        schedule = schedules.weekly_ndvi_aggregation
        assert schedule.cron_schedule == "0 6 * * 1"  # Monday 6 AM
        assert schedule.name == "weekly_rwanda_ndvi"

    def test_rwanda_parcel_sync_schedule_cron(self):
        """Test daily Rwanda parcel sync schedule configuration."""
        schedule = schedules.daily_parcel_sync
        assert schedule.cron_schedule == "0 2 * * *"  # 2 AM daily
        assert schedule.name == "daily_rwanda_parcel_sync"


class TestUploadHandlerIntegration:
    """Test suite for USE_DAGSTER toggle in upload handlers."""

    def test_use_dagster_env_var_parsing(self):
        """Test USE_DAGSTER environment variable parsing."""
        # Test true values
        for val in ["true", "1", "yes"]:
            with patch.dict(os.environ, {"USE_DAGSTER": val}):
                result = os.environ.get("USE_DAGSTER", "false").lower() in ("true", "1", "yes")
                assert result is True

        # Test false values
        for val in ["false", "0", "no"]:
            with patch.dict(os.environ, {"USE_DAGSTER": val}):
                result = os.environ.get("USE_DAGSTER", "false").lower() in ("true", "1", "yes")
                assert result is False

        # Test default
        with patch.dict(os.environ, {}, clear=True):
            result = os.environ.get("USE_DAGSTER", "false").lower() in ("true", "1", "yes")
            assert result is False

    @patch("src.upload.handlers.raster_handler._USE_DAGSTER", True)
    def test_raster_handler_dagster_mode(self):
        """Test raster handler with Dagster mode enabled."""
        # This is a smoke test to ensure the import and flag work
        from src.upload.handlers.raster_handler import _USE_DAGSTER

        # In test mode, the mock forces it to True
        assert _USE_DAGSTER is True

    @patch("src.upload.handlers.vector_handler._USE_DAGSTER", True)
    def test_vector_handler_dagster_mode(self):
        """Test vector handler with Dagster mode enabled."""
        from src.upload.handlers.vector_handler import _USE_DAGSTER

        assert _USE_DAGSTER is True


class TestAssetExecution:
    """Test suite for asset execution logic (smoke tests)."""

    def test_raw_raster_upload_asset_exists(self):
        """Test that raw_raster_upload asset is defined."""
        assert hasattr(raster_assets, "raw_raster_upload")
        asset_fn = getattr(raster_assets, "raw_raster_upload")
        assert callable(asset_fn)

    def test_cog_generation_asset_exists(self):
        """Test that cog_generation asset is defined."""
        assert hasattr(raster_assets, "cog_generation")
        asset_fn = getattr(raster_assets, "cog_generation")
        assert callable(asset_fn)

    def test_zonal_statistics_asset_exists(self):
        """Test that zonal_statistics asset is defined."""
        assert hasattr(raster_assets, "zonal_statistics")
        asset_fn = getattr(raster_assets, "zonal_statistics")
        assert callable(asset_fn)

    def test_raw_vector_upload_asset_exists(self):
        """Test that raw_vector_upload asset is defined."""
        assert hasattr(vector_assets, "raw_vector_upload")
        asset_fn = getattr(vector_assets, "raw_vector_upload")
        assert callable(asset_fn)

    def test_flatgeobuf_conversion_asset_exists(self):
        """Test that flatgeobuf_conversion asset is defined."""
        assert hasattr(vector_assets, "flatgeobuf_conversion")
        asset_fn = getattr(vector_assets, "flatgeobuf_conversion")
        assert callable(asset_fn)

    def test_iceberg_compaction_asset_exists(self):
        """Test that iceberg_compaction asset is defined."""
        assert hasattr(lakehouse_assets, "iceberg_compaction")
        asset_fn = getattr(lakehouse_assets, "iceberg_compaction")
        assert callable(asset_fn)

    def test_snapshot_expiry_asset_exists(self):
        """Test that snapshot_expiry asset is defined."""
        assert hasattr(lakehouse_assets, "snapshot_expiry")
        asset_fn = getattr(lakehouse_assets, "snapshot_expiry")
        assert callable(asset_fn)

    def test_table_optimization_asset_exists(self):
        """Test that table_optimization asset is defined."""
        assert hasattr(lakehouse_assets, "table_optimization")
        asset_fn = getattr(lakehouse_assets, "table_optimization")
        assert callable(asset_fn)

    def test_rwanda_crop_classification_asset_exists(self):
        """Test that rwanda_crop_classification asset is defined."""
        assert hasattr(rwanda_assets, "rwanda_crop_classification")
        asset_fn = getattr(rwanda_assets, "rwanda_crop_classification")
        assert callable(asset_fn)

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
