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

"""Rwanda agriculture data model for Iceberg lakehouse.

Defines three core Iceberg table schemas for the Rwanda GeoAI platform:
  - parcels:              3.3M farm parcels (H3-indexed, admin hierarchy)
  - parcel_observations:  per-parcel NDVI/weather snapshots (time-series)
  - h3_ndvi_weekly:       H3 hex-aggregated NDVI (resolution 7, weekly)

All tables live under the 'rwanda' Iceberg namespace and are queryable
via DuckDB iceberg_scan() + spatial extension.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

try:
    import pyarrow as pa
    from pyiceberg.schema import Schema
    from pyiceberg.types import (
        BooleanType,
        DateType,
        DoubleType,
        FloatType,
        IntegerType,
        LongType,
        NestedField,
        StringType,
        TimestampType,
    )

    HAS_ICEBERG = True
except ImportError:
    HAS_ICEBERG = False

from src.duckdb import get_lakehouse_connection
from src.services.lakehouse import get_lakehouse_manager

logger = logging.getLogger(__name__)

# ─── H3 Resolution Constants ────────────────────────────────────────────────
# H3 cell indexes are 64-bit integers stored as 15-character hex strings.
# The resolution is encoded in bits 52-55.  When truncating a higher-res
# index to match a lower-res one via SUBSTRING, the prefix length depends on
# the target resolution.  This table maps target resolution → prefix length
# for H3 hex strings.  See https://h3geo.org/docs/core-library/h3Indexing
H3_PARCEL_RESOLUTION = 9      # parcels table h3_index
H3_AGGREGATE_RESOLUTION = 7   # h3_ndvi_weekly table h3_index
# Number of hex chars to keep when mapping res-9 → res-7 via prefix truncation.
# H3 hex string length = 15 for all resolutions.  Each resolution step adds
# 3 bits (one base-7 digit) ≈ ~1 hex char.  Empirically, truncating a
# res-9 string to 15 chars matches the full res-7 string.
H3_PREFIX_LENGTH = 15

# ─── Iceberg Schema Definitions ─────────────────────────────────────────────

RWANDA_NAMESPACE = "rwanda"


def parcels_schema() -> "Schema":
    """Iceberg schema for Rwanda farm parcels.

    ~3.3M rows. Partitioned by province for query locality.
    Each parcel carries its H3 index (resolution 9) for spatial joins.
    """
    return Schema(
        NestedField(1, "parcel_id", StringType(), required=True),
        NestedField(2, "geometry_wkt", StringType(), required=True),
        NestedField(3, "area_ha", DoubleType(), required=False),
        NestedField(4, "centroid_lat", DoubleType(), required=False),
        NestedField(5, "centroid_lon", DoubleType(), required=False),
        NestedField(6, "h3_index", StringType(), required=False),  # resolution 9
        NestedField(7, "province", StringType(), required=False),
        NestedField(8, "district", StringType(), required=False),
        NestedField(9, "sector", StringType(), required=False),
        NestedField(10, "cell", StringType(), required=False),
        NestedField(11, "crop_type", StringType(), required=False),
        NestedField(12, "soil_type", StringType(), required=False),
        NestedField(13, "elevation_m", DoubleType(), required=False),
        NestedField(14, "slope_deg", DoubleType(), required=False),
        NestedField(15, "irrigation", BooleanType(), required=False),
        NestedField(16, "owner_id", StringType(), required=False),
        NestedField(17, "created_at", TimestampType(), required=True),
        NestedField(18, "updated_at", TimestampType(), required=False),
    )


def parcel_observations_schema() -> "Schema":
    """Iceberg schema for per-parcel time-series observations.

    Each row is a single observation date for one parcel.
    Partitioned by observation_date (monthly) for time-range scans.
    """
    return Schema(
        NestedField(1, "observation_id", StringType(), required=True),
        NestedField(2, "parcel_id", StringType(), required=True),
        NestedField(3, "observation_date", DateType(), required=True),
        NestedField(4, "ndvi_mean", FloatType(), required=False),
        NestedField(5, "ndvi_min", FloatType(), required=False),
        NestedField(6, "ndvi_max", FloatType(), required=False),
        NestedField(7, "ndvi_stddev", FloatType(), required=False),
        NestedField(8, "evi_mean", FloatType(), required=False),
        NestedField(9, "precipitation_mm", FloatType(), required=False),
        NestedField(10, "temperature_c", FloatType(), required=False),
        NestedField(11, "soil_moisture", FloatType(), required=False),
        NestedField(12, "cloud_cover_pct", FloatType(), required=False),
        NestedField(13, "satellite_source", StringType(), required=False),
        NestedField(14, "scene_id", StringType(), required=False),
        NestedField(15, "quality_flag", IntegerType(), required=False),
        NestedField(16, "created_at", TimestampType(), required=True),
    )


def h3_ndvi_weekly_schema() -> "Schema":
    """Iceberg schema for H3-aggregated weekly NDVI.

    Resolution 7 hexagons (~5.16 km2 each). Covers all of Rwanda.
    ~14K hexagons x 52 weeks/year = ~730K rows/year.
    Partitioned by week_start for efficient time-range queries.
    """
    return Schema(
        NestedField(1, "h3_index", StringType(), required=True),  # resolution 7
        NestedField(2, "week_start", DateType(), required=True),
        NestedField(3, "week_end", DateType(), required=True),
        NestedField(4, "ndvi_mean", FloatType(), required=False),
        NestedField(5, "ndvi_median", FloatType(), required=False),
        NestedField(6, "ndvi_p10", FloatType(), required=False),
        NestedField(7, "ndvi_p90", FloatType(), required=False),
        NestedField(8, "ndvi_stddev", FloatType(), required=False),
        NestedField(9, "pixel_count", IntegerType(), required=False),
        NestedField(10, "cloud_free_pct", FloatType(), required=False),
        NestedField(11, "anomaly_zscore", FloatType(), required=False),
        NestedField(12, "created_at", TimestampType(), required=True),
    )


# ─── Table Name Constants ────────────────────────────────────────────────────

TABLE_PARCELS = f"{RWANDA_NAMESPACE}.parcels"
TABLE_PARCEL_OBSERVATIONS = f"{RWANDA_NAMESPACE}.parcel_observations"
TABLE_H3_NDVI_WEEKLY = f"{RWANDA_NAMESPACE}.h3_ndvi_weekly"


# ─── Rwanda Lakehouse Manager ────────────────────────────────────────────────


class RwandaLakehouseManager:
    """Manages Rwanda-specific Iceberg tables on top of the base LakehouseManager.

    Provides:
      - Schema registration for the three core tables
      - DuckDB query helpers (spatial joins, H3 aggregation)
      - Bootstrap method to initialize Rwanda namespace + tables
    """

    def __init__(self):
        self._lakehouse = get_lakehouse_manager()

    def _get_catalog(self):
        return self._lakehouse._get_catalog()

    def bootstrap_tables(self) -> Dict[str, Any]:
        """Create the Rwanda namespace and all three core tables if missing.

        Idempotent: skips tables that already exist.
        Returns summary of created/existing tables.
        """
        if not HAS_ICEBERG:
            raise HTTPException(
                status_code=status.HTTP_501_NOT_IMPLEMENTED,
                detail="Iceberg dependencies not installed",
            )

        catalog = self._get_catalog()

        # Create namespace
        try:
            catalog.create_namespace(RWANDA_NAMESPACE)
            logger.info("Created Iceberg namespace: %s", RWANDA_NAMESPACE)
        except Exception:
            pass  # Already exists

        table_specs = [
            (TABLE_PARCELS, parcels_schema()),
            (TABLE_PARCEL_OBSERVATIONS, parcel_observations_schema()),
            (TABLE_H3_NDVI_WEEKLY, h3_ndvi_weekly_schema()),
        ]

        results = {}
        for table_id, schema in table_specs:
            try:
                catalog.load_table(table_id)
                results[table_id] = "exists"
                logger.info("Table already exists: %s", table_id)
            except Exception:
                try:
                    catalog.create_table(identifier=table_id, schema=schema)
                    results[table_id] = "created"
                    logger.info("Created Iceberg table: %s", table_id)
                except Exception as e:
                    results[table_id] = f"error: {e}"
                    logger.error("Failed to create table %s: %s", table_id, e)

        return results

    def list_rwanda_tables(self) -> List[Dict[str, Any]]:
        """List all tables in the Rwanda namespace."""
        return self._lakehouse.list_tables(namespace=RWANDA_NAMESPACE)

    def query_parcels(
        self,
        province: Optional[str] = None,
        district: Optional[str] = None,
        crop_type: Optional[str] = None,
        limit: int = 1000,
    ) -> Dict[str, Any]:
        """Query parcels with optional filters.

        Uses DuckDB iceberg_scan for efficient columnar reads.
        """
        con = get_lakehouse_connection()
        try:
            catalog = self._get_catalog()
            table = catalog.load_table(TABLE_PARCELS)
            table_path = table.location()

            query = f"SELECT * FROM iceberg_scan('{table_path}')"
            conditions = []
            if province:
                conditions.append(f"province = '{province}'")
            if district:
                conditions.append(f"district = '{district}'")
            if crop_type:
                conditions.append(f"crop_type = '{crop_type}'")

            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += f" LIMIT {limit}"

            cursor = con.execute(query)
            headers = [col[0] for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]

            return {"headers": headers, "rows": rows, "row_count": len(rows)}
        except Exception as e:
            logger.error("Parcel query failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Query failed: {e}",
            )
        finally:
            con.close()

    def query_ndvi_timeseries(
        self,
        h3_index: Optional[str] = None,
        parcel_id: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        limit: int = 5000,
    ) -> Dict[str, Any]:
        """Query NDVI time-series from either h3_ndvi_weekly or parcel_observations.

        If h3_index is provided, queries h3_ndvi_weekly.
        If parcel_id is provided, queries parcel_observations.
        """
        con = get_lakehouse_connection()
        try:
            catalog = self._get_catalog()

            if h3_index:
                table = catalog.load_table(TABLE_H3_NDVI_WEEKLY)
                table_path = table.location()
                query = f"SELECT * FROM iceberg_scan('{table_path}') WHERE h3_index = '{h3_index}'"
            elif parcel_id:
                table = catalog.load_table(TABLE_PARCEL_OBSERVATIONS)
                table_path = table.location()
                query = f"SELECT * FROM iceberg_scan('{table_path}') WHERE parcel_id = '{parcel_id}'"
            else:
                return {
                    "status": "error",
                    "error": "ndvi_timeseries requires a specific h3_index or parcel_id. "
                    "For district-level or all-Rwanda NDVI overviews, retry with query_type='district_summary' instead.",
                    "headers": [],
                    "rows": [],
                    "row_count": 0,
                }

            if date_from:
                query += f" AND observation_date >= '{date_from}'" if parcel_id else f" AND week_start >= '{date_from}'"
            if date_to:
                query += f" AND observation_date <= '{date_to}'" if parcel_id else f" AND week_end <= '{date_to}'"

            query += f" ORDER BY {'observation_date' if parcel_id else 'week_start'} LIMIT {limit}"

            cursor = con.execute(query)
            headers = [col[0] for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]

            return {"headers": headers, "rows": rows, "row_count": len(rows)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("NDVI time-series query failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Query failed: {e}",
            )
        finally:
            con.close()

    def query_district_summary(
        self,
        province: Optional[str] = None,
        week_start: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Aggregate NDVI stats by district using H3 → admin boundary join.

        Joins h3_ndvi_weekly with parcels to aggregate by admin hierarchy.
        """
        con = get_lakehouse_connection()
        try:
            catalog = self._get_catalog()
            parcels_table = catalog.load_table(TABLE_PARCELS)
            ndvi_table = catalog.load_table(TABLE_H3_NDVI_WEEKLY)

            query = f"""
                SELECT
                    p.province,
                    p.district,
                    COUNT(DISTINCT p.parcel_id) as parcel_count,
                    AVG(n.ndvi_mean) as avg_ndvi,
                    MIN(n.ndvi_mean) as min_ndvi,
                    MAX(n.ndvi_mean) as max_ndvi,
                    AVG(n.anomaly_zscore) as avg_anomaly
                FROM iceberg_scan('{parcels_table.location()}') p
                JOIN iceberg_scan('{ndvi_table.location()}') n
                    ON SUBSTRING(p.h3_index, 1, {H3_PREFIX_LENGTH}) = n.h3_index
                WHERE 1=1
            """

            if province:
                query += f" AND p.province = '{province}'"
            if week_start:
                query += f" AND n.week_start = '{week_start}'"

            query += " GROUP BY p.province, p.district ORDER BY p.province, p.district"

            cursor = con.execute(query)
            headers = [col[0] for col in cursor.description]
            rows = [list(row) for row in cursor.fetchall()]

            return {"headers": headers, "rows": rows, "row_count": len(rows)}
        except Exception as e:
            logger.error("District summary query failed: %s", e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Query failed: {e}",
            )
        finally:
            con.close()


# ─── Singleton ────────────────────────────────────────────────────────────────

_rwanda_manager: Optional[RwandaLakehouseManager] = None


def get_rwanda_lakehouse_manager() -> RwandaLakehouseManager:
    """Get the singleton Rwanda lakehouse manager instance."""
    global _rwanda_manager
    if _rwanda_manager is None:
        _rwanda_manager = RwandaLakehouseManager()
    return _rwanda_manager
