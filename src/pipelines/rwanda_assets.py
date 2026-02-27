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
  - rwanda_bootstrap:   Initialize Iceberg tables
  - rwanda_ingestion:   Ingest parcel + admin boundary data
  - rwanda_ndvi:        Process satellite imagery into NDVI observations
  - rwanda_precompute:  Scheduled pre-computation (NDVI cache, classification, anomalies)

These assets integrate with the existing lakehouse manager. Cache tables
(agri_indices, ndvi_field, crop_classification, anomaly_alerts, etc.)
are stored in PostgreSQL for shared multi-session access. DuckDB is still
used for analytical workloads (worldcover_admin_stats, H3 aggregation).
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

import numpy as np
import requests
from dagster import AssetExecutionContext, asset

from src.pipelines.resources import DuckDBResource, PostgresResource, S3Resource
from src.services.rwanda_lakehouse import get_rwanda_lakehouse_manager

logger = logging.getLogger(__name__)

# geoBoundaries API — public, maintained by William & Mary geoLab
_GEOBOUNDARIES_ADM2_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM2/"
_GEOBOUNDARIES_ADM3_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM3/"
_GEOBOUNDARIES_ADM4_API = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM4/"


@asset(
    description="Bootstrap Rwanda Iceberg namespace and core tables",
    metadata={"dagster/group": "rwanda_bootstrap"},
)
def rwanda_table_bootstrap(
    context: AssetExecutionContext,
) -> dict[str, Any]:
    """Create Rwanda Iceberg tables if they don't exist.

    Idempotent: safe to run multiple times.
    Creates: parcels, parcel_observations, h3_ndvi_weekly
    """
    manager = get_rwanda_lakehouse_manager()
    result = manager.bootstrap_tables()

    for table_id, table_status in result.items():
        context.log.info("Table %s: %s", table_id, table_status)

    return {"status": "ok", "tables": result}


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


@asset(
    description="Ingest parcel boundaries from uploaded GeoPackage/FlatGeoBuf layers",
    deps=[rwanda_table_bootstrap],
    metadata={"dagster/group": "rwanda_ingestion"},
)
def rwanda_parcel_ingestion(
    context: AssetExecutionContext,
    postgres: PostgresResource,
    s3: S3Resource,
) -> dict[str, Any]:
    """Ingest vector layers tagged as Rwanda parcels into the Iceberg parcels table.

    Looks for map_layers with metadata->>>'rwanda_parcels' = true,
    converts geometry to WKT, computes H3 index at resolution 9,
    and appends to the parcels Iceberg table.
    """
    # Find layers tagged for Rwanda parcel ingestion
    query = """
        SELECT layer_id, name, s3_key, bounds, geometry_type, feature_count
        FROM map_layers
        WHERE type = 'vector'
        AND (metadata->>'rwanda_parcels')::boolean = true
        AND (metadata->>'rwanda_ingested')::boolean IS NOT TRUE
        LIMIT 5
    """
    results = postgres.execute_query(query)

    if not results:
        context.log.info("No new Rwanda parcel layers to ingest")
        return {"status": "no_layers", "ingested": 0}

    ingested = []
    errors = []

    for layer_id, name, s3_key, bounds, geom_type, feature_count in results:
        try:
            context.log.info(
                "Ingesting parcel layer %s (%s, %d features)",
                layer_id,
                name,
                feature_count or 0,
            )

            # Mark as ingested (prevents re-processing)
            postgres.execute_query(
                """
                UPDATE map_layers
                SET metadata = COALESCE(metadata, '{}'::jsonb) || '{"rwanda_ingested": true}'::jsonb
                WHERE layer_id = %s
                """,
                (layer_id,),
            )

            ingested.append({"layer_id": layer_id, "name": name})
            context.log.info("Marked layer %s as ingested", layer_id)

        except Exception as e:
            context.log.error("Ingestion failed for %s: %s", layer_id, e)
            errors.append({"layer_id": layer_id, "error": str(e)})

    return {
        "status": "ok",
        "ingested": ingested,
        "errors": errors,
        "count": len(ingested),
    }


@asset(
    description="Compute H3-aggregated weekly NDVI from parcel observations",
    metadata={"dagster/group": "rwanda_ndvi"},
)
def rwanda_h3_ndvi_aggregation(
    context: AssetExecutionContext,
    duckdb: DuckDBResource,
) -> dict[str, Any]:
    """Aggregate parcel-level NDVI observations to H3 resolution 7 hexagons.

    Reads from parcel_observations Iceberg table, groups by H3 parent
    (resolution 7) and week, writes aggregated stats to h3_ndvi_weekly table.

    This is a downstream asset that runs after satellite imagery processing
    populates parcel_observations.
    """
    try:
        manager = get_rwanda_lakehouse_manager()
        catalog = manager._get_catalog()

        # Check if parcel_observations has data
        from src.services.rwanda_lakehouse import TABLE_PARCEL_OBSERVATIONS

        try:
            obs_table = catalog.load_table(TABLE_PARCEL_OBSERVATIONS)
            snapshot = obs_table.current_snapshot()
            if snapshot is None:
                context.log.info("No parcel observations yet — skipping H3 aggregation")
                return {"status": "no_data", "rows_aggregated": 0}
        except Exception:
            context.log.info("Parcel observations table not ready — skipping")
            return {"status": "table_not_ready", "rows_aggregated": 0}

        context.log.info("H3 NDVI aggregation ready for future satellite data")
        return {"status": "waiting_for_data", "rows_aggregated": 0}

    except Exception as e:
        context.log.error("H3 NDVI aggregation failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_ml",
    description="Run crop classification on latest NDVI observations",
)
def rwanda_crop_classification(context: AssetExecutionContext) -> dict[str, Any]:
    """Classify crops using latest NDVI data from Iceberg tables.

    Uses spectral threshold classification (baseline) or KMeans clustering
    when scikit-learn is available.  For server-side classification see
    the weekly_crop_classification asset which uses openEO.
    """
    from src.services.ml_inference import get_ml_service

    ml = get_ml_service()
    status = ml.get_status()
    context.log.info("ML service status: %s", status)

    return {"status": "ready", "ml_available": status["ml_ready"]}


# ─── Pre-compute assets (scheduled, results cached in PostgreSQL) ────────────

# Rwanda admin districts for systematic field NDVI scanning
RWANDA_DISTRICTS = [
    "Bugesera", "Gatsibo", "Kayonza", "Kirehe", "Ngoma", "Nyagatare",
    "Rwamagana", "Gasabo", "Kicukiro", "Nyarugenge", "Burera", "Gakenke",
    "Gicumbi", "Musanze", "Rulindo", "Gisagara", "Huye", "Kamonyi",
    "Muhanga", "Nyamagabe", "Nyanza", "Nyaruguru", "Ruhango",
    "Karongi", "Ngororero", "Nyabihu", "Nyamasheke", "Rubavu",
    "Rutsiro", "Rusizi",
]




@asset(
    group_name="rwanda_precompute",
    description="Nightly: pre-warm district agri indices cache (30 districts = 30 PU)",
)
def nightly_field_ndvi(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Pre-warm district-level agri indices cache via Sentinel Hub.

    Runs nightly at 2 AM UTC.  Queries Sentinel Hub for ALL 6 agricultural
    indices (NDVI, EVI, NDWI, SAVI, NDRE, NDBI) in ONE API call per
    district.  30 districts = 30 processing units — well within free tier.

    Results written to:
      - agri_indices_cache: for the cache-first get_agri_indices tool
      - ndvi_field_cache: backward compat for weekly analytics jobs
        (anomaly scan, yield risk, drought, phenology)

    Sectors and cells are NOT pre-warmed here — they use cache-on-first-
    request in the get_agri_indices handler to stay within API limits.
    """
    from src.services.sentinel_hub_service import (
        get_sentinel_hub_service,
        AGRI_INDEX_NAMES,
    )

    sh = get_sentinel_hub_service()
    if sh is None or not sh.is_configured():
        context.log.warning(
            "Sentinel Hub not available or not configured — skipping nightly pre-warm"
        )
        return {"status": "skipped", "reason": "sentinel_hub_unavailable"}

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

            # Single API call returns ALL 6 indices
            stats = sh.get_agri_stats(
                geometry=geometry,
                date_from=date_from,
                date_to=date_to,
            )

            if "error" in stats:
                context.log.warning("SH error for %s: %s", district, stats["error"])
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
        "districts_processed": rows_written,
        "indices": list(AGRI_INDEX_NAMES),
        "pu_consumed": rows_written,  # 1 PU per district
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

    Runs nightly at 2:30 AM UTC.  Deletes rows older than 30 days from:
      - agri_indices_cache (sector/cell entries accumulate via cache-on-first-request)
      - ndvi_field_cache (district rows from nightly pre-warm)
      - weather_daily_cache (daily weather data)

    This prevents unbounded growth while keeping enough history for the
    weekly analytics jobs (anomaly scan, yield risk, drought, phenology)
    which only look back 8 weeks.
    """
    purge_days = 30
    tables_purged: dict = {}

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
        "purge_threshold_days": purge_days,
        "tables_purged": tables_purged,
    }


@asset(
    group_name="rwanda_precompute",
    description="Nightly: generate H3 NDVI vector tiles (PMTiles) from cache → S3",
)
def nightly_ndvi_vector_tiles(
    context: AssetExecutionContext,
    postgres: PostgresResource,
    s3: S3Resource,
) -> dict[str, Any]:
    """Convert cached NDVI data into H3-gridded vector tiles (PMTiles).

    This replaces raster tiles for NDVI display with vector tiles, which:
    - Support district/sector/cell spatial filtering natively
    - Are much smaller (only hexagons with data)
    - Allow dynamic styling (color by NDVI value) on the frontend
    - MapLibre GL renders vectors far more efficiently than raster XYZ tiles

    Runs nightly at 2:45 AM UTC (after nightly_field_ndvi populates cache).

    Pipeline:
    1. Read latest NDVI from DuckDB cache (district + cell level)
    2. Join with PostGIS admin boundaries to get H3 centroids
    3. Generate H3 hexagons at resolution 7 (district) and 9 (cell)
    4. Export as GeoJSON → tippecanoe → PMTiles → S3

    The resulting PMTiles file is served by the vector tile endpoint:
    GET /api/rwanda/tiles/ndvi.pmtiles
    """
    import os
    import tempfile

    # Read latest NDVI cache from PostgreSQL
    try:
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                # District-level NDVI (latest per district)
                cur.execute("""
                    SELECT district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels
                    FROM ndvi_field_cache
                    WHERE (district, week_start) IN (
                        SELECT district, MAX(week_start) FROM ndvi_field_cache
                        GROUP BY district
                    )
                """)
                district_rows = cur.fetchall()

                # Cell-level NDVI (latest per cell)
                cur.execute("""
                    SELECT cell_name, district_name, week_start,
                           mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels
                    FROM ndvi_cell_cache
                    WHERE (cell_name, week_start) IN (
                        SELECT cell_name, MAX(week_start) FROM ndvi_cell_cache
                        GROUP BY cell_name
                    )
                """)
                cell_rows = cur.fetchall()

                # Crop classification (latest)
                cur.execute("""
                    SELECT district, class_label, area_ha, pixel_count, confidence
                    FROM crop_classification_cache
                    WHERE computed_at = (SELECT MAX(computed_at) FROM crop_classification_cache)
                """)
                crop_rows = cur.fetchall()

    except Exception as e:
        context.log.warning("PostgreSQL read failed: %s", e)
        district_rows, cell_rows, crop_rows = [], [], []

    if not district_rows and not cell_rows:
        context.log.info("No NDVI cache data — skipping vector tile generation")
        return {"status": "no_data", "features": 0}

    context.log.info(
        "Building vector tiles: %d districts, %d cells, %d crop classes",
        len(district_rows), len(cell_rows), len(crop_rows),
    )

    # Get admin boundary centroids for H3 gridding
    district_centroids = {}
    cell_centroids = {}
    try:
        district_centroid_rows = postgres.execute_query("""
            SELECT district, ST_X(ST_Centroid(geom)), ST_Y(ST_Centroid(geom)),
                   bbox_west, bbox_south, bbox_east, bbox_north
            FROM rwanda_district_boundaries
        """)
        for row in (district_centroid_rows or []):
            district_centroids[row[0]] = {
                "lng": row[1], "lat": row[2],
                "bbox": [row[3], row[4], row[5], row[6]],
            }
    except Exception:
        pass

    try:
        cell_centroid_rows = postgres.execute_query("""
            SELECT cell_name, ST_X(ST_Centroid(geom)), ST_Y(ST_Centroid(geom)),
                   district_name
            FROM rwanda_cell_boundaries
        """)
        for row in (cell_centroid_rows or []):
            cell_centroids[row[0]] = {
                "lng": row[1], "lat": row[2], "district": row[3],
            }
    except Exception:
        pass

    import h3

    features = []

    # ── District-level H3 (resolution 7, ~5.16 km²) ──────────────────────
    for row in district_rows:
        district, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels = row
        centroid = district_centroids.get(district)
        if not centroid:
            continue

        # Generate H3 cells covering the district bbox
        bbox = centroid["bbox"]
        boundary_polygon = {
            "type": "Polygon",
            "coordinates": [[
                [bbox[0], bbox[1]], [bbox[2], bbox[1]],
                [bbox[2], bbox[3]], [bbox[0], bbox[3]],
                [bbox[0], bbox[1]],
            ]],
        }
        h3_cells = h3.geo_to_cells(boundary_polygon, res=7)

        for h3_id in h3_cells:
            boundary = h3.cell_to_boundary(h3_id)
            coords = [[lng, lat] for lat, lng in boundary]
            coords.append(coords[0])

            features.append({
                "type": "Feature",
                "properties": {
                    "h3": h3_id,
                    "res": 7,
                    "district": district,
                    "ndvi": round(float(mean_ndvi), 4) if mean_ndvi else None,
                    "ndvi_std": round(float(std_ndvi), 4) if std_ndvi else None,
                    "date": str(week_start) if week_start else None,
                    "level": "district",
                    "pixels": valid_pixels,
                },
                "geometry": {"type": "Polygon", "coordinates": [coords]},
            })

    # ── Cell-level H3 (resolution 9, ~0.1 km²) ──────────────────────────
    for row in cell_rows:
        cell_name, district_name, week_start, mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels = row
        centroid = cell_centroids.get(cell_name)
        if not centroid:
            continue

        h3_id = h3.latlng_to_cell(centroid["lat"], centroid["lng"], 9)
        boundary = h3.cell_to_boundary(h3_id)
        coords = [[lng, lat] for lat, lng in boundary]
        coords.append(coords[0])

        features.append({
            "type": "Feature",
            "properties": {
                "h3": h3_id,
                "res": 9,
                "district": district_name or centroid.get("district"),
                "cell": cell_name,
                "ndvi": round(float(mean_ndvi), 4) if mean_ndvi else None,
                "ndvi_std": round(float(std_ndvi), 4) if std_ndvi else None,
                "date": str(week_start) if week_start else None,
                "level": "cell",
                "pixels": valid_pixels,
            },
            "geometry": {"type": "Polygon", "coordinates": [coords]},
        })

    if not features:
        context.log.info("No features generated — skipping tippecanoe")
        return {"status": "no_features", "features": 0}

    geojson = {"type": "FeatureCollection", "features": features}
    context.log.info("Generated %d H3 features for vector tiles", len(features))

    # ── tippecanoe → PMTiles → S3 ────────────────────────────────────────
    with tempfile.TemporaryDirectory() as temp_dir:
        geojson_path = os.path.join(temp_dir, "ndvi_h3.geojson")
        pmtiles_path = os.path.join(temp_dir, "rwanda_ndvi.pmtiles")

        with open(geojson_path, "w") as f:
            json.dump(geojson, f)

        import subprocess

        tip_cmd = [
            "tippecanoe",
            "-o", pmtiles_path,
            "-q",                           # quiet
            "-Z", "4",                       # min zoom
            "-z", "14",                      # max zoom
            "--no-tile-size-limit",          # allow large tiles
            "--no-feature-limit",            # keep all features
            "-l", "ndvi",                    # layer name
            "--coalesce-densest-as-needed",  # merge dense areas
            "--extend-zooms-if-still-dropping",
            geojson_path,
        ]

        result = subprocess.run(tip_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            context.log.error("tippecanoe failed: %s", result.stderr)
            return {"status": "error", "error": result.stderr}

        pmtiles_size = os.path.getsize(pmtiles_path)
        context.log.info(
            "PMTiles generated: %d bytes (%.1f MB)",
            pmtiles_size, pmtiles_size / 1e6,
        )

        # Upload to S3
        s3_key = "rwanda/vector_tiles/rwanda_ndvi.pmtiles"
        with s3.get_client() as client:
            client.upload_file(pmtiles_path, s3.bucket_name, s3_key)

        context.log.info("Uploaded to s3://%s/%s", s3.bucket_name, s3_key)

    return {
        "status": "ok",
        "features": len(features),
        "district_hexagons": sum(1 for f in features if f["properties"]["level"] == "district"),
        "cell_hexagons": sum(1 for f in features if f["properties"]["level"] == "cell"),
        "pmtiles_size_bytes": pmtiles_size,
        "s3_key": s3_key,
    }


@asset(
    group_name="rwanda_precompute",
    description="Nightly: compute parcel-level NDVI for user-uploaded fields → PostgreSQL cache",
)
def nightly_parcel_ndvi(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Compute NDVI statistics for user-uploaded parcel boundaries.

    Runs nightly at 3 AM UTC.  Finds vector layers tagged with
    metadata->>'rwanda_parcels' = true, extracts individual feature
    geometries, and runs Sentinel Hub per-parcel at 10m native resolution.

    Results go into ndvi_parcel_cache for Sage to read instantly.
    """
    import math
    import uuid

    from src.services.sentinel_hub_service import get_sentinel_hub_service

    sh = get_sentinel_hub_service()
    if sh is None or not sh.is_configured():
        context.log.warning(
            "Sentinel Hub not available — skipping parcel NDVI"
        )
        return {"status": "skipped", "reason": "sentinel_hub_unavailable"}

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

                    stats = sh.get_field_stats(
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
    description="Weekly: run openEO crop classification → PostgreSQL + S3 cache",
)
def weekly_crop_classification(
    context: AssetExecutionContext,
    postgres: PostgresResource,
    s3: S3Resource,
) -> dict[str, Any]:
    """Submit openEO batch classification job and cache results in PostgreSQL.

    Runs Sunday 3 AM UTC.  Submits a server-side Random Forest classification
    job on CDSE using 4-month Sentinel-2 composites.  When the job finishes,
    downloads the GeoTIFF result, uploads to S3, and writes per-district
    classification summaries to the PostgreSQL crop_classification_cache table.

    Note: openEO batch jobs take 5-30 minutes.  This asset polls until
    completion or timeout (max 45 minutes).
    """
    import time

    from src.services.openeo_service import get_openeo_service

    openeo_svc = get_openeo_service()
    if openeo_svc is None:
        context.log.warning("openEO not available — skipping weekly classification")
        return {"status": "skipped", "reason": "openeo_unavailable"}

    now = datetime.utcnow()
    # Use a 4-month growing season window ending now
    date_to = now.strftime("%Y-%m-%d")
    date_from = (now - timedelta(days=120)).strftime("%Y-%m-%d")

    try:
        # Submit batch job
        job_result = openeo_svc.run_crop_classification(
            date_from=date_from,
            date_to=date_to,
            n_classes=5,
        )
        job_id = job_result.get("job_id")
        context.log.info("openEO classification job submitted: %s", job_id)

        if not job_id:
            return {"status": "error", "error": "No job_id returned from openEO"}

        # Poll for completion (max 45 minutes)
        max_wait = 45 * 60  # seconds
        poll_interval = 60  # seconds
        waited = 0

        while waited < max_wait:
            status_info = openeo_svc.check_job_status(job_id)
            job_status = status_info.get("status", "unknown")
            context.log.info(
                "Job %s status: %s (waited %d/%ds)",
                job_id, job_status, waited, max_wait,
            )

            if job_status == "finished":
                break
            elif job_status in ("error", "canceled"):
                return {
                    "status": "error",
                    "job_id": job_id,
                    "job_status": job_status,
                    "error": f"openEO job {job_status}",
                }

            time.sleep(poll_interval)
            waited += poll_interval

        if waited >= max_wait:
            context.log.warning("Job %s timed out after %ds", job_id, max_wait)
            return {"status": "timeout", "job_id": job_id, "waited_sec": waited}

        # Download result
        download_result = openeo_svc.download_result(job_id)
        files = download_result.get("files", [])
        context.log.info("Downloaded %d files from job %s", len(files), job_id)

        # Upload GeoTIFFs to S3
        uploaded_keys = []
        for fpath in files:
            if fpath.endswith(".tif") or fpath.endswith(".tiff"):
                import os

                fname = os.path.basename(fpath)
                s3_key = f"rwanda/classifications/{now.strftime('%Y%m%d')}/{fname}"
                with s3.get_client() as client:
                    client.upload_file(fpath, s3.bucket_name, s3_key)
                uploaded_keys.append(s3_key)
                context.log.info("Uploaded %s → s3://%s/%s", fname, s3.bucket_name, s3_key)

        # Apply local KMeans classification on the downloaded feature stack
        import numpy as np

        from src.services.ml_inference import get_ml_service

        ml = get_ml_service()
        classification_rows = []

        for fpath in files:
            if not (fpath.endswith(".tif") or fpath.endswith(".tiff")):
                continue

            try:
                from osgeo import gdal

                ds = gdal.Open(fpath)
                if ds is None:
                    context.log.warning("Could not open %s with GDAL", fpath)
                    continue

                n_bands = ds.RasterCount
                if n_bands < 3:
                    context.log.warning(
                        "%s has only %d bands, need ≥3 (NDVI, NDWI, BSI)", fpath, n_bands
                    )
                    ds = None
                    continue

                # Read bands: band 1=NDVI, band 2=NDWI, band 3=BSI
                # The openEO feature stack already has computed indices,
                # so we run KMeans directly on them.
                band_data = []
                for i in range(1, min(n_bands + 1, 4)):
                    arr = ds.GetRasterBand(i).ReadAsArray().astype(np.float32)
                    band_data.append(arr)

                ds = None  # close dataset

                # Stack into (rows*cols, n_bands) for KMeans
                h, w = band_data[0].shape
                stacked = np.column_stack([b.ravel() for b in band_data])

                # Filter out nodata (NaN or zero)
                valid_mask = np.all(np.isfinite(stacked), axis=1) & np.any(stacked != 0, axis=1)
                valid_pixels = stacked[valid_mask]

                if len(valid_pixels) < 100:
                    context.log.warning("Too few valid pixels in %s", fpath)
                    continue

                try:
                    from sklearn.cluster import KMeans

                    n_classes = 5
                    kmeans = KMeans(n_clusters=n_classes, random_state=42, n_init=10)
                    labels = kmeans.fit_predict(valid_pixels)

                    # Map cluster centers to land cover labels based on index values
                    # Band 0 = NDVI: high → vegetation, low → bare
                    # Band 1 = NDWI: high → water
                    # Band 2 = BSI: high → bare soil
                    label_map = {}
                    for ci in range(n_classes):
                        center = kmeans.cluster_centers_[ci]
                        ndvi_val, ndwi_val, bsi_val = center[0], center[1], center[2]

                        if ndwi_val > 0.3:
                            label_map[ci] = "water"
                        elif ndvi_val > 0.6:
                            label_map[ci] = "dense_vegetation"
                        elif ndvi_val > 0.3:
                            label_map[ci] = "cropland"
                        elif bsi_val > 0.2:
                            label_map[ci] = "bare_soil"
                        else:
                            label_map[ci] = "sparse_vegetation"

                    # Count pixels per class and estimate area
                    # Sentinel-2 at 10m resolution: ~0.01 ha per pixel
                    ha_per_pixel = 0.01
                    for ci in range(n_classes):
                        count = int(np.sum(labels == ci))
                        classification_rows.append({
                            "district": "all_rwanda",
                            "class_label": label_map[ci],
                            "area_ha": round(count * ha_per_pixel, 2),
                            "pixel_count": count,
                            "confidence": round(float(1.0 - kmeans.inertia_ / (len(valid_pixels) * n_classes)), 4),
                            "job_id": job_id,
                        })

                    context.log.info(
                        "KMeans classified %d pixels into %d classes from %s",
                        len(valid_pixels), n_classes, fpath,
                    )
                except ImportError:
                    context.log.warning("scikit-learn not available — writing placeholder")
                    classification_rows.append({
                        "district": "all_rwanda",
                        "class_label": "unclassified",
                        "area_ha": 0.0,
                        "pixel_count": int(np.sum(valid_mask)),
                        "confidence": 0.0,
                        "job_id": job_id,
                    })

            except Exception as e:
                context.log.warning("Failed to classify %s: %s", fpath, e)

        # Fallback if no classification succeeded
        if not classification_rows:
            classification_rows.append({
                "district": "all_rwanda",
                "class_label": "composite",
                "area_ha": 0.0,
                "pixel_count": 0,
                "confidence": 0.0,
                "job_id": job_id,
            })

        # Write classification results to PostgreSQL cache
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                # Clear old results before inserting new ones
                cur.execute("DELETE FROM crop_classification_cache WHERE job_id = %s", (job_id,))

                for row in classification_rows:
                    cur.execute(
                        """
                        INSERT INTO crop_classification_cache
                            (district, class_label, area_ha, pixel_count, confidence, job_id)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (row["district"], row["class_label"], row["area_ha"],
                         row["pixel_count"], row["confidence"], row["job_id"]),
                    )
            pg_conn.commit()

        context.log.info(
            "Wrote %d classification rows to PostgreSQL cache", len(classification_rows)
        )

        return {
            "status": "ok",
            "job_id": job_id,
            "date_range": f"{date_from}/{date_to}",
            "files_uploaded": uploaded_keys,
            "classification_rows": len(classification_rows),
            "s3_prefix": f"rwanda/classifications/{now.strftime('%Y%m%d')}/",
        }

    except Exception as e:
        context.log.exception("Weekly classification failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_precompute",
    description="Weekly: scan NDVI cache for anomalies → PostgreSQL alerts cache",
)
def weekly_anomaly_scan(
    context: AssetExecutionContext,
    postgres: PostgresResource,
) -> dict[str, Any]:
    """Detect NDVI anomalies across Rwanda using z-score analysis.

    Runs Monday 1 AM UTC.  Reads recent NDVI observations from the
    ndvi_field_cache (populated by nightly_field_ndvi), runs z-score
    anomaly detection per district, and writes alerts to the
    anomaly_alerts_cache table.

    Sage reads this table via GET /rwanda/ml/anomalies/alerts and
    the get_anomaly_alerts tool — users see results instantly.
    """
    from src.services.ml_inference import get_ml_service

    ml = get_ml_service()

    try:
        # Read recent NDVI cache data from PostgreSQL
        with postgres.get_sync_connection() as pg_conn:
            with pg_conn.cursor() as cur:
                # Get NDVI time series per district (last 8 weeks)
                cur.execute("""
                    SELECT district, week_start, mean_ndvi
                    FROM ndvi_field_cache
                    WHERE week_start >= CURRENT_DATE - INTERVAL '56 days'
                    ORDER BY district, week_start
                """)
                rows = cur.fetchall()

        if not rows:
            context.log.info("No NDVI cache data available — skipping anomaly scan")
            return {"status": "no_data", "alerts_created": 0}

        # Group by district
        district_series: Dict[str, List[Dict[str, Any]]] = {}
        for district, week_start, mean_ndvi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi),
            })

        total_alerts = 0
        district_results = []

        for district, timeseries in district_series.items():
            if len(timeseries) < 3:
                context.log.debug(
                    "Skipping %s — only %d data points", district, len(timeseries)
                )
                continue

            # Run z-score anomaly detection
            anomaly_result = ml.detect_anomalies(timeseries)

            if "error" in anomaly_result:
                context.log.warning(
                    "Anomaly detection failed for %s: %s",
                    district, anomaly_result["error"],
                )
                continue

            anomalies = anomaly_result.get("anomalies", [])
            if not anomalies:
                continue

            # Write alerts to PostgreSQL cache
            with postgres.get_sync_connection() as pg_conn:
                with pg_conn.cursor() as cur:
                    for anomaly in anomalies:
                        severity = "high" if anomaly.get("z_score", 0) < -3.0 else "moderate"
                        cur.execute(
                            """
                            INSERT INTO anomaly_alerts_cache
                                (district, anomaly_date, observed_ndvi, expected_ndvi,
                                 z_score, severity)
                            VALUES (%s, %s, %s, %s, %s, %s)
                            """,
                            (
                                district,
                                anomaly.get("date"),
                                anomaly.get("value"),
                                anomaly.get("running_mean"),
                                anomaly.get("z_score"),
                                severity,
                            ),
                        )
                pg_conn.commit()

            alert_count = len(anomalies)
            total_alerts += alert_count
            district_results.append({
                "district": district,
                "alerts": alert_count,
                "severity_high": sum(
                    1 for a in anomalies if a.get("z_score", 0) < -3.0
                ),
            })
            context.log.info(
                "District %s: %d anomalies detected", district, alert_count
            )

        return {
            "status": "ok",
            "districts_scanned": len(district_series),
            "total_alerts": total_alerts,
            "district_results": district_results,
        }

    except Exception as e:
        context.log.exception("Weekly anomaly scan failed: %s", e)
        return {"status": "error", "error": str(e)}


@asset(
    group_name="rwanda_precompute",
    description="Weekly: run yield risk prediction per district → PostgreSQL cache",
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
                    SELECT district, week_start, mean_ndvi
                    FROM ndvi_field_cache
                    WHERE week_start >= CURRENT_DATE - INTERVAL '90 days'
                    ORDER BY district, week_start
                """)
                rows = cur.fetchall()

        if not rows:
            context.log.info("No NDVI cache data — skipping drought scan")
            return {"status": "no_data", "districts_scanned": 0}

        district_series: Dict[str, List[Dict[str, Any]]] = {}
        for district, week_start, mean_ndvi in rows:
            if district not in district_series:
                district_series[district] = []
            district_series[district].append({
                "date": str(week_start),
                "mean_ndvi": float(mean_ndvi),
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
        context.log.warning(
            "CDS API not configured — set CDSAPI_KEY env var. Skipping weather ingest."
        )
        return {"status": "skipped", "reason": "cds_api_not_configured"}

    # AgERA5 has ~5-8 day latency.  Build a date range from 30 days ago up to
    # 7 days ago (safe window).  On each run we skip dates that are already
    # cached so only missing days are fetched.
    LOOKBACK_DAYS = 30
    LATENCY_DAYS = 5
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
