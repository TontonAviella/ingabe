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

"""Dagster assets for Rwanda agriculture data pipelines.

Asset groups:
  - rwanda_bootstrap:    Load admin boundaries (district, sector, cell) into PostGIS
  - rwanda_precompute:   Scheduled pre-computation (NDVI cache, drought, weather)
  - rwanda_admin_index:  H3 <-> admin unit index

Cache tables (agri_indices, ndvi_field, drought, etc.) are stored in
PostgreSQL for shared multi-session access. DuckDB is still used for
analytical workloads (worldcover_admin_stats).
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np
import requests
from dagster import AssetExecutionContext, asset

from src.pipelines.resources import DuckDBResource, PostgresResource
from src.pipelines.posthog_observability import observed_dagster_asset
from src.services.admin_boundaries import RWANDA_DISTRICTS

logger = logging.getLogger(__name__)

# geoBoundaries API — public, maintained by William & Mary geoLab
_GEOBOUNDARIES_ADM2_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM2/"
_GEOBOUNDARIES_ADM3_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM3/"
_GEOBOUNDARIES_ADM4_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM4/"


@asset(
    group_name="rwanda_bootstrap",
    description="ETL: Fetch Rwanda district boundaries from geoBoundaries API → PostGIS",
)
def rwanda_admin_boundaries(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Extract Rwanda ADM2 district boundaries from geoBoundaries public API.

    Creates ``rwanda_district_boundaries`` PostGIS table with real geometries
    for all 30 districts.  Idempotent — skips if already populated.
    Source: geoBoundaries (CC-BY-4.0), William & Mary geoLab.
    """
    # Check if already populated
    try:
        existing = postgres.execute_query(
            "SELECT COUNT(*) FROM rwanda_district_boundaries"
        )
        if existing and existing[0][0] >= 30:
            context.log.info("rwanda_district_boundaries already has %d rows", existing[0][0])
            return {"status": "exists", "districts": existing[0][0]}
    except Exception:
        pass  # Table doesn't exist yet

    # ── Extract ───────────────────────────────────────────────────────────
    context.log.info("Fetching Rwanda ADM2 from geoBoundaries API...")
    try:
        api_resp = requests.get(_GEOBOUNDARIES_ADM2_API, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            return {"status": "error", "error": "No gjDownloadURL in API response"}

        geojson_resp = requests.get(geojson_url, timeout=120)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as e:
        context.log.error("Failed to fetch boundaries: %s", e)
        return {"status": "error", "error": str(e)}

    context.log.info("Downloaded %d district features", len(features))

    # ── Transform + Load ──────────────────────────────────────────────────
    with postgres.get_sync_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rwanda_district_boundaries (
                    district VARCHAR PRIMARY KEY,
                    geom GEOMETRY(MultiPolygon, 4326),
                    bbox_west DOUBLE PRECISION,
                    bbox_south DOUBLE PRECISION,
                    bbox_east DOUBLE PRECISION,
                    bbox_north DOUBLE PRECISION
                )
            """)
            cur.execute("DELETE FROM rwanda_district_boundaries")

            loaded = 0
            for feat in features:
                name = feat["properties"].get("shapeName")
                if not name:
                    continue
                geom_json = json.dumps(feat["geometry"])
                cur.execute(
                    """
                    INSERT INTO rwanda_district_boundaries
                        (district, geom, bbox_west, bbox_south, bbox_east, bbox_north)
                    VALUES (
                        %s,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                        ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(%s)))
                    )
                    """,
                    (name, geom_json, geom_json, geom_json, geom_json, geom_json),
                )
                loaded += 1

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rwanda_districts_geom
                ON rwanda_district_boundaries USING GIST (geom)
            """)
            conn.commit()

    context.log.info("Loaded %d district boundaries into PostGIS", loaded)
    return {"status": "ok", "districts_loaded": loaded}


@asset(
    group_name="rwanda_bootstrap",
    description="ETL: Fetch Rwanda ADM3 sector boundaries from geoBoundaries API → PostGIS",
)
def rwanda_sector_boundaries(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Extract Rwanda ADM3 sector boundaries from geoBoundaries public API.

    Creates ``rwanda_sector_boundaries`` PostGIS table with real geometries
    for all ~416 sectors.  Idempotent — skips if already populated.
    Source: geoBoundaries (CC-BY-4.0), William & Mary geoLab.
    """
    # Check if already populated
    try:
        existing = postgres.execute_query(
            "SELECT COUNT(*) FROM rwanda_sector_boundaries"
        )
        if existing and existing[0][0] >= 400:
            context.log.info("rwanda_sector_boundaries already has %d rows", existing[0][0])
            return {"status": "exists", "sectors": existing[0][0]}
    except Exception:
        pass  # Table doesn't exist yet

    # ── Extract ───────────────────────────────────────────────────────────
    context.log.info("Fetching Rwanda ADM3 from geoBoundaries API...")
    try:
        api_resp = requests.get(_GEOBOUNDARIES_ADM3_API, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            return {"status": "error", "error": "No gjDownloadURL in API response"}

        geojson_resp = requests.get(geojson_url, timeout=180)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as e:
        context.log.error("Failed to fetch ADM3 boundaries: %s", e)
        return {"status": "error", "error": str(e)}

    context.log.info("Downloaded %d sector features", len(features))

    # ── Transform + Load ──────────────────────────────────────────────────
    with postgres.get_sync_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rwanda_sector_boundaries (
                    sector_id SERIAL PRIMARY KEY,
                    sector_name VARCHAR,
                    district_name VARCHAR,
                    geom GEOMETRY(MultiPolygon, 4326),
                    area_km2 DOUBLE PRECISION,
                    bbox_west DOUBLE PRECISION,
                    bbox_south DOUBLE PRECISION,
                    bbox_east DOUBLE PRECISION,
                    bbox_north DOUBLE PRECISION
                )
            """)
            cur.execute("DELETE FROM rwanda_sector_boundaries")

            loaded = 0
            for feat in features:
                props = feat.get("properties", {})
                sector_name = props.get("shapeName", "")
                # geoBoundaries ADM3 nests the parent district in shapeGroup
                district_name = props.get("shapeGroup", "")

                if not sector_name:
                    continue

                geom_json = json.dumps(feat["geometry"])
                cur.execute(
                    """
                    INSERT INTO rwanda_sector_boundaries
                        (sector_name, district_name, geom, area_km2,
                         bbox_west, bbox_south, bbox_east, bbox_north)
                    VALUES (
                        %s, %s,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                        ST_Area(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 32736)) / 1e6,
                        ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(%s)))
                    )
                    """,
                    (sector_name, district_name,
                     geom_json, geom_json, geom_json, geom_json, geom_json, geom_json),
                )
                loaded += 1

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_geom
                ON rwanda_sector_boundaries USING GIST (geom)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_district
                ON rwanda_sector_boundaries (LOWER(district_name))
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_name
                ON rwanda_sector_boundaries (LOWER(sector_name))
            """)

            # Back-fill district_name from spatial join if geoBoundaries
            # didn't provide it (or provided the wrong parent level)
            try:
                cur.execute("""
                    UPDATE rwanda_sector_boundaries s
                    SET district_name = d.district
                    FROM rwanda_district_boundaries d
                    WHERE ST_Within(ST_Centroid(s.geom), d.geom)
                      AND (s.district_name IS NULL OR s.district_name = '')
                """)
                context.log.info("Back-filled district_name from spatial join")
            except Exception as e:
                context.log.warning("Could not back-fill district_name: %s", e)

            conn.commit()

    context.log.info("Loaded %d sector boundaries into PostGIS", loaded)
    return {"status": "ok", "sectors_loaded": loaded}


@asset(
    group_name="rwanda_bootstrap",
    description="ETL: Fetch Rwanda ADM4 cell boundaries from geoBoundaries API → PostGIS",
)
def rwanda_cell_boundaries(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Extract Rwanda ADM4 cell boundaries from geoBoundaries public API.

    Creates ``rwanda_cell_boundaries`` PostGIS table with real geometries
    for all ~2,148 cells.  Idempotent — skips if already populated.
    Source: geoBoundaries (CC-BY-4.0), William & Mary geoLab.
    """
    # Check if already populated
    try:
        existing = postgres.execute_query(
            "SELECT COUNT(*) FROM rwanda_cell_boundaries"
        )
        if existing and existing[0][0] >= 2000:
            context.log.info("rwanda_cell_boundaries already has %d rows", existing[0][0])
            return {"status": "exists", "cells": existing[0][0]}
    except Exception:
        pass  # Table doesn't exist yet

    # ── Extract ───────────────────────────────────────────────────────────
    context.log.info("Fetching Rwanda ADM4 from geoBoundaries API...")
    try:
        api_resp = requests.get(_GEOBOUNDARIES_ADM4_API, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            return {"status": "error", "error": "No gjDownloadURL in API response"}

        geojson_resp = requests.get(geojson_url, timeout=300)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as e:
        context.log.error("Failed to fetch ADM4 boundaries: %s", e)
        return {"status": "error", "error": str(e)}

    context.log.info("Downloaded %d cell features", len(features))

    # ── Transform + Load ──────────────────────────────────────────────────
    with postgres.get_sync_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS rwanda_cell_boundaries (
                    cell_id SERIAL PRIMARY KEY,
                    cell_name VARCHAR,
                    sector_name VARCHAR,
                    district_name VARCHAR,
                    geom GEOMETRY(MultiPolygon, 4326),
                    area_km2 DOUBLE PRECISION,
                    bbox_west DOUBLE PRECISION,
                    bbox_south DOUBLE PRECISION,
                    bbox_east DOUBLE PRECISION,
                    bbox_north DOUBLE PRECISION
                )
            """)
            cur.execute("DELETE FROM rwanda_cell_boundaries")

            loaded = 0
            for feat in features:
                props = feat.get("properties", {})
                # geoBoundaries ADM4 features have shapeName for cell name
                cell_name = props.get("shapeName", "")
                # Parent admin names — geoBoundaries nests these in properties
                sector_name = props.get("shapeGroup", "")
                district_name = props.get("shapeGroup", "")

                if not cell_name:
                    continue

                geom_json = json.dumps(feat["geometry"])
                cur.execute(
                    """
                    INSERT INTO rwanda_cell_boundaries
                        (cell_name, sector_name, district_name, geom, area_km2,
                         bbox_west, bbox_south, bbox_east, bbox_north)
                    VALUES (
                        %s, %s, %s,
                        ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326)),
                        ST_Area(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(%s), 4326), 32736)) / 1e6,
                        ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(%s))),
                        ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(%s)))
                    )
                    """,
                    (cell_name, sector_name, district_name,
                     geom_json, geom_json, geom_json, geom_json, geom_json, geom_json),
                )
                loaded += 1

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_rwanda_cells_geom
                ON rwanda_cell_boundaries USING GIST (geom)
            """)

            # Back-fill district_name from spatial join with districts table
            try:
                cur.execute("""
                    UPDATE rwanda_cell_boundaries c
                    SET district_name = d.district
                    FROM rwanda_district_boundaries d
                    WHERE ST_Within(ST_Centroid(c.geom), d.geom)
                """)
                context.log.info("Back-filled district_name from spatial join")
            except Exception as e:
                context.log.warning("Could not back-fill district_name: %s", e)

            conn.commit()

    context.log.info("Loaded %d cell boundaries into PostGIS", loaded)
    return {"status": "ok", "cells_loaded": loaded}


# ─── Pre-compute assets (scheduled, results cached in PostgreSQL) ────────────

# Rwanda admin districts for systematic field NDVI scanning (owned by
# src/services/admin_boundaries.py).




@asset(
    group_name="rwanda_precompute",
    description="Nightly: pre-warm district agri indices cache (30 districts = 30 PU)",
)
@observed_dagster_asset(
    asset_name="nightly_field_ndvi",
    pipeline_family="satellite_agri_indices",
    source_category="satellite",
    analysis_domain="agriculture",
    evidence_kind="district_ndvi_cache",
)
def nightly_field_ndvi(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Pre-warm district-level agri indices cache via Digital Earth Africa.

    Runs nightly at 2 AM UTC.  Reads Sentinel-2 L2A COGs from DE Africa's
    public S3 bucket (free, no credentials, no rate limits) and computes
    ALL 6 agricultural indices (NDVI, EVI, NDWI, SAVI, NDRE, NDBI) per
    district.  30 districts × ~7 band reads each.

    Results written to:
      - agri_indices_cache: for the cache-first get_agri_indices tool
      - ndvi_field_cache: backward compat for weekly analytics jobs
        (yield risk, drought, phenology)

    Sectors and cells are NOT pre-warmed here — they use cache-on-first-
    request in the get_agri_indices handler to stay within API limits.
    """
    from src.services.deafrica_stac import get_deafrica_service

    AGRI_INDEX_NAMES = ["ndvi", "evi", "ndwi", "savi", "ndre", "ndbi"]

    dea = get_deafrica_service()

    # Get all districts from rwanda_district_boundaries
    try:
        district_rows = postgres.execute_query("""
            SELECT district, ST_AsGeoJSON(geom)
            FROM rwanda_district_boundaries
            ORDER BY district
        """)
    except Exception:
        district_rows = []

    if not district_rows:
        context.log.warning("No district boundaries available — run rwanda_admin_boundaries first")
        return {"status": "skipped", "reason": "no_district_boundaries"}

    context.log.info("Pre-warming %d districts with multi-index evalscript", len(district_rows))

    now = datetime.utcnow()
    date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    week_start = date_from

    rows_written = 0
    errors = []

    for district, geom_geojson in district_rows:
        try:
            if not geom_geojson:
                continue

            geometry = json.loads(geom_geojson)

            stats = dea.get_agri_stats(
                geometry=geometry,
                date_from=date_from,
                date_to=date_to,
            )

            if "error" in stats:
                context.log.warning("DE Africa error for %s: %s", district, stats["error"])
                errors.append({"district": district, "error": stats["error"]})
                continue

            intervals = stats.get("intervals", [])
            if not intervals:
                continue

            # Aggregate each index across daily intervals
            index_stats: dict = {}
            total_pixels = 0
            for idx_name in AGRI_INDEX_NAMES:
                means = [
                    iv[idx_name]["mean"]
                    for iv in intervals
                    if idx_name in iv and iv[idx_name].get("valid_pixels", 0) > 0
                ]
                if means:
                    index_stats[f"{idx_name}_mean"] = round(float(np.mean(means)), 4)
                    index_stats[f"{idx_name}_std"] = round(float(np.std(means)), 4)
                else:
                    index_stats[f"{idx_name}_mean"] = None
                    index_stats[f"{idx_name}_std"] = None

            for iv in intervals:
                if "ndvi" in iv:
                    total_pixels += iv["ndvi"].get("valid_pixels", 0)

            with postgres.get_sync_connection() as pg_conn:
                with pg_conn.cursor() as cur:
                    # Write to agri_indices_cache (primary cache for get_agri_indices tool)
                    cur.execute(
                        """
                        INSERT INTO agri_indices_cache
                            (admin_level, admin_name, parent_name, week_start,
                             ndvi_mean, ndvi_std, evi_mean, evi_std,
                             ndwi_mean, ndwi_std, savi_mean, savi_std,
                             ndre_mean, ndre_std, ndbi_mean, ndbi_std,
                             valid_pixels)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            "district", district, None, week_start,
                            index_stats.get("ndvi_mean"), index_stats.get("ndvi_std"),
                            index_stats.get("evi_mean"), index_stats.get("evi_std"),
                            index_stats.get("ndwi_mean"), index_stats.get("ndwi_std"),
                            index_stats.get("savi_mean"), index_stats.get("savi_std"),
                            index_stats.get("ndre_mean"), index_stats.get("ndre_std"),
                            index_stats.get("ndbi_mean"), index_stats.get("ndbi_std"),
                            total_pixels,
                        ),
                    )

                    # Write to ndvi_field_cache (backward compat for weekly analytics)
                    ndvi_mean = index_stats.get("ndvi_mean")
                    ndvi_std = index_stats.get("ndvi_std")
                    if ndvi_mean is not None:
                        cur.execute(
                            """
                            INSERT INTO ndvi_field_cache
                                (district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            """,
                            (district, week_start, ndvi_mean, ndvi_std, ndvi_mean, ndvi_mean, total_pixels),
                        )
                pg_conn.commit()

            rows_written += 1
            context.log.info(
                "Pre-warm: %s NDVI=%.4f EVI=%.4f NDWI=%.4f (%d px)",
                district,
                index_stats.get("ndvi_mean", 0),
                index_stats.get("evi_mean", 0),
                index_stats.get("ndwi_mean", 0),
                total_pixels,
            )

        except Exception as e:
            context.log.error("Failed for district %s: %s", district, e)
            errors.append({"district": district, "error": str(e)})

    return {
        "status": "ok",
        "backend": "deafrica",
        "districts_processed": rows_written,
        "indices": list(AGRI_INDEX_NAMES),
        "errors": errors,
        "date_range": f"{date_from}/{date_to}",
    }


@asset(
    group_name="rwanda_precompute",
    description="Nightly: purge stale PostgreSQL cache entries older than 30 days",
)
def nightly_cache_cleanup(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Purge stale cache entries to keep PostgreSQL lean.

    Runs nightly at 2:30 AM UTC.  Deletes rows older than their retention
    period.  ndvi_field_cache and agri_indices_cache use 365-day retention
    because VCI drought detection requires a multi-year baseline for
    accurate min/max NDVI (~30 districts × 52 weeks = 1,560 rows/year).
    Other caches use 30-day retention.
    """
    tables_purged: dict = {}

    # Tables with 365-day retention (needed for VCI historical baseline)
    long_retention = {"ndvi_field_cache", "agri_indices_cache"}

    with postgres.get_sync_connection() as pg_conn:
        with pg_conn.cursor() as cur:
            for table, ts_col in [
                ("agri_indices_cache", "computed_at"),
                ("ndvi_field_cache", "computed_at"),
                ("weather_daily_cache", "computed_at"),
                ("anomaly_alerts_cache", "computed_at"),
                ("yield_risk_cache", "computed_at"),
                ("drought_cache", "computed_at"),
                ("phenology_cache", "computed_at"),
            ]:
                try:
                    purge_days = 365 if table in long_retention else 30
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    before = cur.fetchone()[0]
                    cur.execute(
                        f"DELETE FROM {table} WHERE {ts_col} < CURRENT_DATE - INTERVAL '{purge_days} days'"
                    )
                    cur.execute(f"SELECT COUNT(*) FROM {table}")
                    after = cur.fetchone()[0]
                    deleted = before - after
                    if deleted > 0:
                        tables_purged[table] = deleted
                        context.log.info("Purged %d rows from %s", deleted, table)
                except Exception as e:
                    # Table may not exist yet — that's fine
                    context.log.debug("Skipping %s: %s", table, e)
        pg_conn.commit()

    context.log.info("Cache cleanup done: %s", tables_purged)
    return {
        "status": "ok",
        "purge_threshold_days": {"default": 30, "ndvi_field_cache": 365, "agri_indices_cache": 365},
        "tables_purged": tables_purged,
    }


@asset(
    group_name="rwanda_precompute",
    description="Nightly: compute parcel-level NDVI for user-uploaded fields → PostgreSQL cache",
)
@observed_dagster_asset(
    asset_name="nightly_parcel_ndvi",
    pipeline_family="satellite_parcel_ndvi",
    source_category="satellite",
    analysis_domain="agriculture",
    evidence_kind="parcel_ndvi_cache",
)
def nightly_parcel_ndvi(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Compute NDVI statistics for user-uploaded parcel boundaries.

    Runs nightly at 3 AM UTC.  Finds vector layers tagged with
    metadata->>'rwanda_parcels' = true, extracts individual feature
    geometries, and computes NDVI via Digital Earth Africa at 10m
    native resolution.

    Results go into ndvi_parcel_cache for Sage to read instantly.
    """
    import math
    import uuid

    from src.services.deafrica_stac import get_deafrica_service

    dea = get_deafrica_service()

    # Find parcel layers: user-uploaded vectors tagged as rwanda_parcels
    try:
        parcel_layers = postgres.execute_query("""
            SELECT layer_id, name
            FROM map_layers
            WHERE type = 'vector'
              AND (metadata->>'rwanda_parcels')::boolean = true
        """)
    except Exception:
        parcel_layers = []

    if not parcel_layers:
        context.log.info("No user-uploaded parcel layers found")
        return {"status": "no_parcels", "parcels_processed": 0}

    context.log.info("Found %d parcel layers to process", len(parcel_layers))

    now = datetime.utcnow()
    date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    total_parcels = 0
    errors = []

    for layer_id, layer_name in parcel_layers:
        try:
            # Try to get individual feature geometries from PostGIS
            # User-uploaded layers store features in a layer-specific table
            # or in the postgis_features table
            feature_rows = []

            # Check for features in the layer's PostGIS table
            try:
                feature_rows = postgres.execute_query(
                    """
                    SELECT
                        COALESCE(properties->>'name', properties->>'id',
                                 properties->>'parcel_id', 'parcel_' || ROW_NUMBER() OVER()),
                        ST_AsGeoJSON(geom)
                    FROM postgis_layer_%s
                    WHERE geom IS NOT NULL
                    LIMIT 500
                    """,
                    (layer_id.replace("-", "_"),),
                )
            except Exception:
                pass

            # Fallback: check if features stored via s3_key FlatGeoBuf
            if not feature_rows:
                context.log.info(
                    "No PostGIS features for layer %s — will process as single geometry",
                    layer_id,
                )
                # Get the layer's bounds as a single geometry
                try:
                    bounds_rows = postgres.execute_query(
                        """
                        SELECT name, ST_AsGeoJSON(
                            ST_MakeEnvelope(
                                (bounds->>'west')::float,
                                (bounds->>'south')::float,
                                (bounds->>'east')::float,
                                (bounds->>'north')::float,
                                4326
                            )
                        )
                        FROM map_layers
                        WHERE layer_id = %s
                        """,
                        (layer_id,),
                    )
                    if bounds_rows:
                        feature_rows = [(bounds_rows[0][0], bounds_rows[0][1])]
                except Exception:
                    pass

            if not feature_rows:
                context.log.warning("No features found for layer %s", layer_id)
                continue

            context.log.info(
                "Processing %d parcels from layer %s (%s)",
                len(feature_rows), layer_id, layer_name,
            )

            for parcel_name, geom_geojson in feature_rows:
                try:
                    if not geom_geojson:
                        continue

                    geometry = json.loads(geom_geojson)

                    stats = dea.get_field_stats(
                        geometry=geometry,
                        date_from=date_from,
                        date_to=date_to,
                        index="ndvi",
                    )

                    if "error" in stats:
                        errors.append({"parcel": parcel_name, "error": stats["error"]})
                        continue

                    intervals = stats.get("intervals", [])
                    if not intervals:
                        continue

                    ndvi_means = [
                        iv["ndvi"]["mean"]
                        for iv in intervals
                        if "ndvi" in iv
                        and iv["ndvi"].get("valid_pixels", 0) > 0
                        and not math.isnan(iv["ndvi"]["mean"])
                    ]
                    if not ndvi_means:
                        continue

                    mean_ndvi = float(np.mean(ndvi_means))
                    std_ndvi = float(np.std(ndvi_means))
                    min_ndvi = float(np.min(ndvi_means))
                    max_ndvi = float(np.max(ndvi_means))
                    total_pixels = sum(
                        iv["ndvi"].get("valid_pixels", 0)
                        for iv in intervals
                        if "ndvi" in iv
                    )

                    # Estimate area from pixel count (10m resolution = 0.01 ha/pixel)
                    area_ha = round(total_pixels * 0.01, 2)

                    parcel_id = str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"{layer_id}/{parcel_name}",
                    ))

                    with postgres.get_sync_connection() as pg_conn:
                        with pg_conn.cursor() as cur:
                            cur.execute(
                                """
                                INSERT INTO ndvi_parcel_cache
                                    (parcel_id, parcel_name, layer_id, week_start,
                                     mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                                     valid_pixels, area_ha)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (parcel_id, parcel_name, str(layer_id), week_start,
                                 mean_ndvi, std_ndvi, min_ndvi, max_ndvi,
                                 total_pixels, area_ha),
                            )
                        pg_conn.commit()

                    total_parcels += 1

                except Exception as e:
                    errors.append({"parcel": parcel_name, "error": str(e)})

        except Exception as e:
            context.log.error("Failed processing layer %s: %s", layer_id, e)
            errors.append({"layer_id": str(layer_id), "error": str(e)})

    context.log.info(
        "Parcel NDVI complete: %d parcels processed, %d errors",
        total_parcels, len(errors),
    )
    return {
        "status": "ok",
        "parcels_processed": total_parcels,
        "layers_checked": len(parcel_layers),
        "errors_count": len(errors),
        "errors": errors[:10],
        "date_range": f"{date_from}/{date_to}",
    }


@asset(
    group_name="rwanda_precompute",
    description="Weekly: run yield risk prediction per district → PostgreSQL cache",
)
@observed_dagster_asset(
    asset_name="weekly_yield_risk",
    pipeline_family="satellite_yield_risk",
    source_category="satellite",
    analysis_domain="agriculture",
    evidence_kind="yield_risk_cache",
)
def weekly_yield_risk(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Predict yield risk per district using Mann-Kendall trend analysis.

    Runs Monday 2 AM UTC.  Reads NDVI time series from ndvi_field_cache
    (populated by nightly_field_ndvi), runs Mann-Kendall trend + Theil-Sen
    slope per district, and writes risk assessments to yield_risk_cache.

    Sage reads this table via the get_yield_risk tool.
    """
    from src.services.ml_inference import get_ml_service

    ml = get_ml_service()

    try:
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT district, week_start, mean_ndvi
                    FROM ndvi_field_cache
                    WHERE week_start >= CURRENT_DATE - INTERVAL '90 days'
                    ORDER BY district, week_start
                """)
                rows = cur.fetchall()

        if not rows:
            context.log.info("No NDVI cache data — skipping yield risk")
            return {"status": "no_data", "districts_assessed": 0}

        # Group by district
        district_series: Dict[str, List[Dict[str, Any]]] = {}
        for district, week_start, mean_ndvi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi),
            })

        assessed = 0
        results = []

        for district, timeseries in district_series.items():
            if len(timeseries) < 3:
                continue

            risk = ml.predict_yield_risk(timeseries)
            if "error" in risk:
                context.log.warning("Yield risk failed for %s: %s", district, risk["error"])
                continue

            with postgres.get_sync_connection() as pg_conn:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO yield_risk_cache
                            (district, risk_level, risk_description, trend_slope,
                             kendall_tau, latest_ndvi, mean_ndvi, seasonal_deviation, observations)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            district,
                            risk.get("risk_level"),
                            risk.get("risk_description"),
                            risk.get("trend_slope"),
                            risk.get("kendall_tau"),
                            risk.get("latest_ndvi"),
                            risk.get("mean_ndvi"),
                            risk.get("seasonal_deviation"),
                            risk.get("observations"),
                        ),
                    )
                pg_conn.commit()

            assessed += 1
            results.append({"district": district, "risk_level": risk.get("risk_level")})
            context.log.info("Yield risk: %s → %s", district, risk.get("risk_level"))

        return {"status": "ok", "districts_assessed": assessed, "results": results}

    except Exception as e:
        context.log.exception("Weekly yield risk failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_precompute",
    description="Weekly: drought detection per district → PostgreSQL cache",
)
@observed_dagster_asset(
    asset_name="weekly_drought_scan",
    pipeline_family="satellite_drought_scan",
    source_category="satellite",
    analysis_domain="agriculture",
    evidence_kind="drought_cache",
)
def weekly_drought_scan(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Detect drought conditions per district using VCI + NDWI analysis.

    Runs Monday 3 AM UTC.  Reads NDVI (and NDWI when available) from
    ndvi_field_cache, computes Vegetation Condition Index per district,
    and writes drought status to drought_cache table.
    """
    from src.services.ml_inference import get_ml_service

    ml = get_ml_service()

    try:
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT n.district, n.week_start, n.mean_ndvi,
                           a.ndwi_mean AS mean_ndwi
                    FROM ndvi_field_cache n
                    LEFT JOIN agri_indices_cache a
                      ON a.admin_level = 'district'
                      AND n.district = a.admin_name
                      AND n.week_start = a.week_start
                    WHERE n.week_start >= CURRENT_DATE - INTERVAL '365 days'
                    ORDER BY n.district, n.week_start
                """)
                rows = cur.fetchall()

        if not rows:
            context.log.info("No NDVI cache data — skipping drought scan")
            return {"status": "no_data", "districts_scanned": 0}

        district_series: Dict[str, List[Dict[str, Any]]] = {}
        for district, week_start, mean_ndvi, mean_ndwi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi),
                "mean_ndwi": float(mean_ndwi) if mean_ndwi is not None else None,
            })

        scanned = 0
        results = []

        for district, timeseries in district_series.items():
            if len(timeseries) < 3:
                continue

            drought = ml.detect_drought(timeseries)
            if "error" in drought:
                continue

            with postgres.get_sync_connection() as pg_conn:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO drought_cache
                            (district, drought_status, current_vci, latest_ndvi,
                             latest_ndwi, drought_period_count, description)
                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            district,
                            drought.get("drought_status"),
                            drought.get("current_vci"),
                            drought.get("latest_ndvi"),
                            drought.get("latest_ndwi"),
                            drought.get("drought_period_count"),
                            drought.get("description"),
                        ),
                    )
                pg_conn.commit()

            scanned += 1
            results.append({"district": district, "status": drought.get("drought_status")})
            context.log.info("Drought: %s → %s (VCI=%.1f)",
                             district, drought.get("drought_status"), drought.get("current_vci", 0))

        return {"status": "ok", "districts_scanned": scanned, "results": results}

    except Exception as e:
        context.log.exception("Weekly drought scan failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_precompute",
    description="Weekly: crop phenology analysis per district → PostgreSQL cache",
)
@observed_dagster_asset(
    asset_name="weekly_phenology",
    pipeline_family="satellite_phenology",
    source_category="satellite",
    analysis_domain="agriculture",
    evidence_kind="phenology_cache",
)
def weekly_phenology(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Identify crop growth stages per district from NDVI phenology curves.

    Runs Monday 4 AM UTC.  Reads NDVI time series from ndvi_field_cache,
    identifies phenological stages (dormant, green_up, peak, senescence,
    harvest) per district, and writes current stage to phenology_cache.
    """
    from src.services.ml_inference import get_ml_service

    ml = get_ml_service()

    try:
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.execute("""
                    SELECT district, week_start, mean_ndvi
                    FROM ndvi_field_cache
                    WHERE week_start >= CURRENT_DATE - INTERVAL '180 days'
                    ORDER BY district, week_start
                """)
                rows = cur.fetchall()

        if not rows:
            context.log.info("No NDVI cache data — skipping phenology")
            return {"status": "no_data", "districts_analyzed": 0}

        district_series: Dict[str, List[Dict[str, Any]]] = {}
        for district, week_start, mean_ndvi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi),
            })

        analyzed = 0
        results = []

        for district, timeseries in district_series.items():
            if len(timeseries) < 4:
                continue

            pheno = ml.analyze_crop_phenology(timeseries)
            if "error" in pheno:
                continue

            with postgres.get_sync_connection() as pg_conn:
                with pg_conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO phenology_cache
                            (district, current_stage, peak_ndvi, peak_date,
                             green_up_start, senescence_start, harvest_date, observations)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            district,
                            pheno.get("current_stage"),
                            pheno.get("peak_ndvi"),
                            pheno.get("peak_date"),
                            pheno.get("green_up_start"),
                            pheno.get("senescence_start"),
                            pheno.get("harvest_date"),
                            pheno.get("observations"),
                        ),
                    )
                pg_conn.commit()

            analyzed += 1
            results.append({"district": district, "stage": pheno.get("current_stage")})
            context.log.info("Phenology: %s → %s", district, pheno.get("current_stage"))

        return {"status": "ok", "districts_analyzed": analyzed, "results": results}

    except Exception as e:
        context.log.exception("Weekly phenology failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_precompute",
    description="Daily: ingest AgERA5 weather data per district -> PostgreSQL cache",
)
@observed_dagster_asset(
    asset_name="daily_weather_ingest",
    pipeline_family="weather_ingest",
    source_category="weather",
    analysis_domain="agriculture",
    evidence_kind="agera5_weather_cache",
)
def daily_weather_ingest(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Download AgERA5 agrometeorological indicators and aggregate to districts.

    Runs daily at 6 AM UTC.  AgERA5 has ~5-day latency, so we download
    data for (today - 7 days) to ensure availability.  Fetches:
      - 2m temperature (mean, max, min) in Celsius
      - Precipitation flux converted to mm/day
      - Solar radiation flux converted to MJ/m2/day

    Results are aggregated to each of the 30 Rwanda districts using
    bounding-box zonal statistics and written to weather_daily_cache.

    Sage reads this table via the get_weather_stats tool.
    """
    from src.services.weather_service import get_weather_service

    ws = get_weather_service()
    if ws is None or not ws.is_configured():
        context.log.error(
            "CDS API not configured — set CDSAPI_KEY env var. Weather cache not updated."
        )
        return {"status": "failed", "reason": "cds_api_not_configured: set CDSAPI_KEY"}

    # AgERA5 2_0 is about 8 days behind (2026-09-26 was the latest day on
    # 2026-10-04).  Build a date range from 30 days ago up to 9 days ago so
    # unpublished days are not requested.  On each run we skip dates that are
    # already cached so only missing days are fetched.
    LOOKBACK_DAYS = 30
    LATENCY_DAYS = 9
    today = datetime.utcnow().date()
    start_date = today - timedelta(days=LOOKBACK_DAYS)
    end_date = today - timedelta(days=LATENCY_DAYS)

    # Find which dates are already cached
    with postgres.get_sync_connection() as pg_conn:
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT observation_date FROM weather_daily_cache "
                "WHERE observation_date >= %s AND observation_date <= %s",
                (str(start_date), str(end_date)),
            )
            cached_dates_raw = cur.fetchall()
    cached_dates = {row[0] for row in cached_dates_raw}

    # Build list of missing dates
    all_dates = []
    d = start_date
    while d <= end_date:
        if d not in cached_dates:
            all_dates.append(d)
        d += timedelta(days=1)

    if not all_dates:
        context.log.info(
            "Weather cache up-to-date: all dates from %s to %s already cached",
            start_date, end_date,
        )
        return {"status": "up_to_date", "range": f"{start_date} to {end_date}"}

    context.log.info(
        "Weather ingest: %d dates to fetch (%s to %s), %d already cached",
        len(all_dates), all_dates[0], all_dates[-1], len(cached_dates),
    )

    # Get district bounding boxes from PostGIS
    try:
        district_rows = postgres.execute_query("""
            SELECT district, bbox_west, bbox_south, bbox_east, bbox_north
            FROM rwanda_district_boundaries
            ORDER BY district
        """)
    except Exception:
        district_rows = []

    if not district_rows:
        context.log.warning(
            "No district boundaries — run rwanda_admin_boundaries first"
        )
        return {"status": "skipped", "reason": "no_district_boundaries"}

    district_geometries = [
        {
            "district": row[0],
            "bbox": (row[1], row[2], row[3], row[4]),
        }
        for row in district_rows
    ]

    total_rows_written = 0
    dates_processed = 0
    errors_list: list[str] = []

    for target_date in all_dates:
        context.log.info(
            "Downloading AgERA5 weather for %s across %d districts (%d/%d)",
            target_date, len(district_geometries), dates_processed + 1, len(all_dates),
        )

        # Download all variables for the target date
        try:
            weather_data = ws.download_agera5_day(target_date)
        except Exception as exc:
            context.log.warning("Download failed for %s: %s", target_date, exc)
            errors_list.append(f"{target_date}: {exc}")
            continue

        if "error" in weather_data and not weather_data.get("variables"):
            context.log.warning("Weather download failed for %s: %s", target_date, weather_data.get("error"))
            errors_list.append(f"{target_date}: {weather_data.get('error')}")
            continue

        if weather_data.get("errors"):
            context.log.warning("Partial errors for %s: %s", target_date, weather_data["errors"])

        # Aggregate to districts
        district_stats = ws.aggregate_to_districts(weather_data, district_geometries)

        if not district_stats:
            context.log.warning("No district stats for %s", target_date)
            continue

        # Write to PostgreSQL cache
        insert_params = [
            (
                stats.get("district"),
                stats.get("date"),
                stats.get("temperature_mean"),
                stats.get("temperature_max"),
                stats.get("temperature_min"),
                stats.get("precipitation"),
                stats.get("solar_radiation"),
            )
            for stats in district_stats
        ]

        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO weather_daily_cache
                        (district, observation_date, temperature_mean, temperature_max,
                         temperature_min, precipitation, solar_radiation)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    insert_params,
                )
            pg_conn.commit()
            total_rows_written += len(insert_params)

        dates_processed += 1
        context.log.info(
            "Weather cache: wrote %d rows for %s (%d/%d dates done)",
            len(insert_params), target_date, dates_processed, len(all_dates),
        )

    context.log.info(
        "Weather ingest complete: %d dates processed, %d total rows written",
        dates_processed, total_rows_written,
    )
    return {
        "status": "ok",
        "dates_processed": dates_processed,
        "total_rows": total_rows_written,
        "range": f"{all_dates[0]} to {all_dates[-1]}" if all_dates else "none",
        "errors": errors_list if errors_list else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# WorldCover Land-Cover Zonal Statistics
# ═══════════════════════════════════════════════════════════════════════════


@asset(
    group_name="rwanda_precompute",
    description=(
        "Pre-compute ESRI 10m Annual LULC 2024 land-cover zonal statistics for every "
        "Rwanda admin boundary (district, sector, cell).  Results are cached in "
        "DuckDB for instant querying by Sage.  Connected-component analysis for "
        "largest cropland regions is done on-the-fly per user query."
    ),
)
def worldcover_zonal_stats(
    context: AssetExecutionContext,
    postgres: PostgresResource,
    duckdb: DuckDBResource,
) -> dict[str, Any]:
    """Memory-efficient LULC zonal stats using per-boundary windowed
    COG reads via WarpedVRT (ESRI tiles are UTM, we need EPSG:4326).

    Instead of loading the full Rwanda mosaic into memory, this approach:
      1.  Opens COG datasets lazily via WarpedVRT (UTM -> EPSG:4326)
      2.  For each boundary, reads only the bbox window via rasterio.merge
      3.  Frees memory after each admin level with gc.collect()
    """
    import gc

    from rasterio.features import geometry_mask
    from rasterio.merge import merge

    from src.worldcover import WORLDCOVER_CLASSES, open_rwanda_datasets_warped

    PIXEL_AREA_HA = 0.01  # 10m x 10m = 100 m^2 = 0.01 hectares

    # ── Step 1: Open COG datasets via WarpedVRT (UTM -> EPSG:4326) ────────
    context.log.info("Opening ESRI LULC COG datasets via WarpedVRT...")
    try:
        wc_pairs = open_rwanda_datasets_warped()
    except Exception as e:
        context.log.error("Failed to open LULC tiles: %s", e)
        return {"status": "error", "error": f"No LULC tiles could be opened: {e}"}

    datasets = [vrt for vrt, _raw in wc_pairs]
    context.log.info("Opened %d COG datasets via WarpedVRT (EPSG:4326)", len(datasets))

    def _read_window(bbox: tuple[float, float, float, float]):
        """Read WorldCover data for a bounding box window from COGs."""
        west, south, east, north = bbox
        buf = 0.001  # small buffer to avoid edge clipping
        bounds = (west - buf, south - buf, east + buf, north + buf)
        arr, tfm = merge(datasets, bounds=bounds)
        return arr[0], tfm  # single band

    # ── Step 2: Zonal stats per boundary (windowed reads) ─────────────────
    context.log.info("Computing zonal stats with per-boundary windowed reads...")

    admin_queries = {
        "district": (
            "SELECT district AS name, NULL AS sector_name, NULL AS district_name, "
            "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM rwanda_district_boundaries"
        ),
        "sector": (
            "SELECT sector_name AS name, sector_name, district_name, "
            "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM rwanda_sector_boundaries"
        ),
        "cell": (
            "SELECT cell_name AS name, sector_name, district_name, "
            "ST_AsGeoJSON(geom)::text, bbox_west, bbox_south, bbox_east, bbox_north "
            "FROM rwanda_cell_boundaries"
        ),
    }

    all_stats_rows: list[list] = []

    for level, sql in admin_queries.items():
        rows = postgres.execute_query(sql)
        if not rows:
            context.log.warning("No rows for level %s", level)
            continue

        context.log.info("Processing %d %s boundaries...", len(rows), level)

        for i, row in enumerate(rows):
            name = row[0]
            if level == "district":
                sector_name, district_name = None, name
            elif level == "sector":
                sector_name, district_name = row[1], row[2]
            else:  # cell
                sector_name, district_name = row[1], row[2]

            geojson_str = row[3]
            bbox = (row[4], row[5], row[6], row[7])

            try:
                geom = json.loads(geojson_str)
                data, tfm = _read_window(bbox)
                h, w = data.shape
                mask = geometry_mask(
                    [geom], out_shape=(h, w), transform=tfm, invert=True
                )
                inside = data[mask]
                if inside.size == 0:
                    continue

                vals, counts = np.unique(inside, return_counts=True)
                for val, cnt in zip(vals, counts):
                    if val == 0:
                        continue
                    class_name = WORLDCOVER_CLASSES.get(int(val), f"unknown_{val}")
                    hectares = round(float(cnt) * PIXEL_AREA_HA, 2)
                    all_stats_rows.append([
                        level, name, district_name, sector_name,
                        int(val), class_name, int(cnt), hectares,
                    ])
            except Exception as e:
                if i < 3:
                    context.log.warning("%s/%s failed: %s", level, name, e)
                continue

            if (i + 1) % 100 == 0:
                context.log.info("  %s: %d/%d done", level, i + 1, len(rows))

        context.log.info("  %s level complete", level)
        gc.collect()

    context.log.info("Computed %d stat rows across all admin levels", len(all_stats_rows))

    # Close COG datasets (both VRT and raw)
    for vrt, raw_ds in wc_pairs:
        vrt.close()
        raw_ds.close()

    # ── Step 3: Write results to DuckDB ───────────────────────────────────
    # Note: Connected-component analysis for largest_cropland queries is
    # now done on-the-fly in message_routes.py for the specific boundary
    # the user asks about. This asset only pre-computes zonal stats.
    context.log.info("Writing results to DuckDB...")
    with duckdb.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS worldcover_admin_stats")
        conn.execute("""
            CREATE TABLE worldcover_admin_stats (
                admin_level VARCHAR,
                admin_name VARCHAR,
                district_name VARCHAR,
                sector_name VARCHAR,
                class_value INTEGER,
                class_name VARCHAR,
                pixel_count INTEGER,
                area_hectares DOUBLE
            )
        """)
        if all_stats_rows:
            conn.executemany(
                "INSERT INTO worldcover_admin_stats VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                all_stats_rows,
            )
        context.log.info("Wrote %d rows to worldcover_admin_stats", len(all_stats_rows))

        # Drop legacy CC table if it exists (no longer pre-computed)
        conn.execute("DROP TABLE IF EXISTS worldcover_cropland_regions")

    return {
        "status": "ok",
        "admin_stat_rows": len(all_stats_rows),
        "total_districts": sum(1 for r in all_stats_rows if r[0] == "district"),
        "total_sectors": sum(1 for r in all_stats_rows if r[0] == "sector"),
        "total_cells": sum(1 for r in all_stats_rows if r[0] == "cell"),
    }


@asset(
    group_name="rwanda_admin_index",
    description=(
        "H3 admin index: every resolution-9 hexagon matched to province, district, "
        "sector, cell and village by shared area (h3_admin_overlap, h3_admin_cells). "
        "Run after boundaries change."
    ),
)
def rwanda_h3_admin_index(context: AssetExecutionContext) -> dict[str, Any]:
    from src.services.h3_admin_index import build_from_env

    summary = asyncio.run(build_from_env())
    context.log.info("H3 admin index: %s", summary)
    return summary
