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

"""Dagster pipeline definitions for Ingabe.

This module serves as the entry point for Dagster, exposing all assets,
sensors, schedules, and resources defined in the pipelines package.

Phase 3: Dagster Pipeline Orchestration
- Raster processing (COG generation, zonal stats)
- Vector processing (FlatGeoBuf, PMTiles, Iceberg)
- Lakehouse maintenance (compaction, snapshot expiry, optimization)
- Event-driven triggers (S3 upload sensor)
- Scheduled jobs (hourly compaction, daily expiry, weekly optimization)

Requires: dagster package. If not installed, this module exports HAS_DAGSTER=False
and the rest of the app continues to work without pipeline orchestration.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from dagster import (
        AssetSelection,
        Definitions,
        define_asset_job,
        load_assets_from_modules,
    )

    from src.pipelines import (
        hooks,
        lakehouse_assets,
        raster_assets,
        resources,
        rwanda_assets,
        schedules,
        sensors,
        vector_assets,
    )

    HAS_DAGSTER = True
except ImportError:
    HAS_DAGSTER = False
    defs = None
    logger.info("Dagster not installed — pipeline orchestration disabled")

if HAS_DAGSTER:
    # ─── Load all assets from modules ───────────────────────────────────────
    raster_asset_list = load_assets_from_modules([raster_assets])
    vector_asset_list = load_assets_from_modules([vector_assets])
    lakehouse_asset_list = load_assets_from_modules([lakehouse_assets])
    rwanda_asset_list = load_assets_from_modules([rwanda_assets])

    all_assets = [
        *raster_asset_list,
        *vector_asset_list,
        *lakehouse_asset_list,
        *rwanda_asset_list,
    ]

    # ─── Define jobs for specific asset groups ─────────────────────────────
    raster_processing_job = define_asset_job(
        name="raster_processing_job",
        description="Process raster uploads: COG generation and zonal statistics",
        selection=AssetSelection.groups("raster_processing"),
        tags={"category": "raster"},
    )

    vector_processing_job = define_asset_job(
        name="vector_processing_job",
        description="Process vector uploads: FlatGeoBuf, PMTiles, Iceberg registration",
        selection=AssetSelection.groups("vector_processing"),
        tags={"category": "vector"},
    )

    iceberg_compaction_job = define_asset_job(
        name="iceberg_compaction_job",
        description="Compact Iceberg table data files",
        selection=AssetSelection.assets(lakehouse_assets.iceberg_compaction),
        tags={"category": "lakehouse", "operation": "compaction"},
    )

    snapshot_expiry_job = define_asset_job(
        name="snapshot_expiry_job",
        description="Expire old Iceberg snapshots",
        selection=AssetSelection.assets(lakehouse_assets.snapshot_expiry),
        tags={"category": "lakehouse", "operation": "snapshot_expiry"},
    )

    table_optimization_job = define_asset_job(
        name="table_optimization_job",
        description="Optimize Iceberg table layout",
        selection=AssetSelection.assets(lakehouse_assets.table_optimization),
        tags={"category": "lakehouse", "operation": "optimization"},
    )

    cache_warmup_job = define_asset_job(
        name="cache_warmup_job",
        description="Warm Redis tile cache for popular layers",
        selection=AssetSelection.groups("lakehouse_maintenance"),
        tags={"category": "cache"},
    )

    rwanda_bootstrap_job = define_asset_job(
        name="rwanda_bootstrap_job",
        description="Bootstrap Rwanda Iceberg tables",
        selection=AssetSelection.groups("rwanda_bootstrap"),
        tags={"category": "rwanda"},
    )

    rwanda_ingestion_job = define_asset_job(
        name="rwanda_ingestion_job",
        description="Ingest Rwanda parcel data into Iceberg tables",
        selection=AssetSelection.groups("rwanda_ingestion"),
        tags={"category": "rwanda"},
    )

    rwanda_ndvi_job = define_asset_job(
        name="rwanda_ndvi_job",
        description="Aggregate NDVI data to H3 hexagons",
        selection=AssetSelection.groups("rwanda_ndvi"),
        tags={"category": "rwanda"},
    )

    rwanda_ml_job = define_asset_job(
        name="rwanda_ml_job",
        description="Run ML crop classification",
        selection=AssetSelection.groups("rwanda_ml"),
        tags={"category": "rwanda", "ml": "true"},
    )

    # Rwanda pre-compute jobs (scheduled — results cached in DuckDB for Sage)
    nightly_field_ndvi_job = define_asset_job(
        name="nightly_field_ndvi_job",
        description="Nightly NDVI field stats via Sentinel Hub → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.nightly_field_ndvi),
        tags={"category": "rwanda", "precompute": "true"},
    )

    weekly_crop_classification_job = define_asset_job(
        name="weekly_crop_classification_job",
        description="Weekly openEO crop classification → DuckDB + S3",
        selection=AssetSelection.assets(rwanda_assets.weekly_crop_classification),
        tags={"category": "rwanda", "precompute": "true"},
    )

    weekly_anomaly_scan_job = define_asset_job(
        name="weekly_anomaly_scan_job",
        description="Weekly NDVI anomaly detection → DuckDB alerts",
        selection=AssetSelection.assets(rwanda_assets.weekly_anomaly_scan),
        tags={"category": "rwanda", "precompute": "true"},
    )

    weekly_yield_risk_job = define_asset_job(
        name="weekly_yield_risk_job",
        description="Weekly yield risk prediction → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.weekly_yield_risk),
        tags={"category": "rwanda", "precompute": "true"},
    )

    weekly_drought_scan_job = define_asset_job(
        name="weekly_drought_scan_job",
        description="Weekly drought detection → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.weekly_drought_scan),
        tags={"category": "rwanda", "precompute": "true"},
    )

    weekly_phenology_job = define_asset_job(
        name="weekly_phenology_job",
        description="Weekly crop phenology analysis → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.weekly_phenology),
        tags={"category": "rwanda", "precompute": "true"},
    )

    nightly_cache_cleanup_job = define_asset_job(
        name="nightly_cache_cleanup_job",
        description="Nightly: purge stale DuckDB cache entries older than 30 days",
        selection=AssetSelection.assets(rwanda_assets.nightly_cache_cleanup),
        tags={"category": "rwanda", "precompute": "true"},
    )

    nightly_parcel_ndvi_job = define_asset_job(
        name="nightly_parcel_ndvi_job",
        description="Nightly parcel-level NDVI for user-uploaded fields → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.nightly_parcel_ndvi),
        tags={"category": "rwanda", "precompute": "true"},
    )

    daily_weather_ingest_job = define_asset_job(
        name="daily_weather_ingest_job",
        description="Daily AgERA5 weather data → district aggregation → DuckDB cache",
        selection=AssetSelection.assets(rwanda_assets.daily_weather_ingest),
        tags={"category": "rwanda", "precompute": "true"},
    )

    # ─── Define resource instances ──────────────────────────────────────────
    resource_defs = {
        "s3": resources.S3Resource.from_env(),
        "postgres": resources.PostgresResource.from_env(),
        "redis": resources.RedisResource.from_env(),
        "duckdb": resources.DuckDBResource(database_path="/tmp/ingabe_cache/cache.duckdb"),
    }

    # ─── Build sensors (need job references) ────────────────────────────────
    s3_upload_sensor = sensors.build_s3_upload_sensor(
        raster_job=raster_processing_job,
        vector_job=vector_processing_job,
    )
    failed_cog_retry_sensor = sensors.build_failed_cog_retry_sensor(
        raster_job=raster_processing_job,
    )
    satellite_scene_sensor = sensors.build_satellite_scene_sensor()

    # ─── Collect all jobs ───────────────────────────────────────────────────
    all_jobs = [
        raster_processing_job,
        vector_processing_job,
        iceberg_compaction_job,
        snapshot_expiry_job,
        table_optimization_job,
        cache_warmup_job,
        rwanda_bootstrap_job,
        rwanda_ingestion_job,
        rwanda_ndvi_job,
        rwanda_ml_job,
        nightly_field_ndvi_job,
        weekly_crop_classification_job,
        weekly_anomaly_scan_job,
        weekly_yield_risk_job,
        weekly_drought_scan_job,
        weekly_phenology_job,
        nightly_cache_cleanup_job,
        nightly_parcel_ndvi_job,
        daily_weather_ingest_job,
    ]

    # ─── Define Dagster Definitions ────────────────────────────────────────
    # Note: hooks are defined in hooks.py but cannot be attached via
    # with_hooks() on UnresolvedAssetJobDefinition. Attach per-asset
    # in a future PR.
    defs = Definitions(
        assets=all_assets,
        jobs=all_jobs,
        sensors=[
            s3_upload_sensor,
            failed_cog_retry_sensor,
            satellite_scene_sensor,
        ],
        schedules=[
            schedules.compaction_schedule,
            schedules.snapshot_expiry_schedule,
            schedules.cache_warmup_schedule,
            schedules.table_optimization_schedule,
            schedules.weekly_ndvi_aggregation,
            schedules.daily_parcel_sync,
            schedules.nightly_field_ndvi_schedule,
            schedules.weekly_classification_schedule,
            schedules.weekly_anomaly_schedule,
            schedules.weekly_yield_risk_schedule,
            schedules.weekly_drought_schedule,
            schedules.weekly_phenology_schedule,
            schedules.nightly_cache_cleanup_schedule,
            schedules.nightly_parcel_ndvi_schedule,
            schedules.daily_weather_ingest_schedule,
        ],
        resources=resource_defs,
    )

    logger.info("Dagster definitions loaded successfully")
    logger.info("Assets: %d", len(all_assets))
    logger.info("Jobs: 18, Sensors: 3, Schedules: 14")

# Export for workspace.yaml reference
__all__ = ["defs", "HAS_DAGSTER"]
