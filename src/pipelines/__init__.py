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

Scheduled Rwanda jobs:
- Admin boundary bootstrap and the H3 <-> admin index
- Pre-compute caches Sage reads (field/parcel NDVI, anomalies, yield risk,
  drought, phenology, weather) and their nightly cleanup

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
        in_process_executor,
        load_assets_from_modules,
    )

    from src.pipelines import (
        resources,
        rwanda_assets,
        schedules,
    )

    HAS_DAGSTER = True
except ImportError:
    HAS_DAGSTER = False
    defs = None
    logger.info("Dagster not installed — pipeline orchestration disabled")

if HAS_DAGSTER:
    # ─── Load all assets from modules ───────────────────────────────────────
    all_assets = load_assets_from_modules([rwanda_assets])

    # ─── Define jobs ────────────────────────────────────────────────────────
    h3_admin_index_job = define_asset_job(
        name="h3_admin_index_job",
        description="Rebuild the H3 admin index (hexagon <-> village/cell/sector/district/province)",
        selection=AssetSelection.assets(rwanda_assets.rwanda_h3_admin_index),
        tags={"category": "rwanda"},
    )

    rwanda_bootstrap_job = define_asset_job(
        name="rwanda_bootstrap_job",
        description="Load Rwanda admin boundaries (district, sector, cell) into PostGIS",
        selection=AssetSelection.groups("rwanda_bootstrap"),
        tags={"category": "rwanda"},
    )

    # Rwanda pre-compute jobs (scheduled — results cached in DuckDB for Sage)
    nightly_field_ndvi_job = define_asset_job(
        name="nightly_field_ndvi_job",
        description="Nightly NDVI field stats via Digital Earth Africa STAC → cache",
        selection=AssetSelection.assets(rwanda_assets.nightly_field_ndvi),
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
        "postgres": resources.PostgresResource.from_env(),
        "duckdb": resources.DuckDBResource(database_path="/tmp/ingabe_cache/cache.duckdb"),
    }

    # ─── Collect all jobs ───────────────────────────────────────────────────
    all_jobs = [
        rwanda_bootstrap_job,
        h3_admin_index_job,
        nightly_field_ndvi_job,
        weekly_yield_risk_job,
        weekly_drought_scan_job,
        weekly_phenology_job,
        nightly_cache_cleanup_job,
        nightly_parcel_ndvi_job,
        daily_weather_ingest_job,
    ]

    all_schedules = [
        schedules.nightly_field_ndvi_schedule,
        schedules.weekly_yield_risk_schedule,
        schedules.weekly_drought_schedule,
        schedules.weekly_phenology_schedule,
        schedules.nightly_cache_cleanup_schedule,
        schedules.nightly_parcel_ndvi_schedule,
        schedules.daily_weather_ingest_schedule,
    ]

    # ─── Define Dagster Definitions ────────────────────────────────────────
    defs = Definitions(
        assets=all_assets,
        jobs=all_jobs,
        schedules=all_schedules,
        resources=resource_defs,
        # One process per run, not one per step: each step subprocess reloads
        # all definitions (~430 MB), and runs execute one at a time under the
        # daemon's 1.5 GB cap (dagster.yaml, docker-compose.yml).
        executor=in_process_executor,
    )

    logger.info("Dagster definitions loaded successfully")
    logger.info("Assets: %d", len(all_assets))
    logger.info("Jobs: %d, Schedules: %d", len(all_jobs), len(all_schedules))

# Export for workspace.yaml reference
__all__ = ["defs", "HAS_DAGSTER"]
