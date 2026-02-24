#!/usr/bin/env python3
"""Manual trigger for daily weather ingest.

Usage: docker compose exec -T app python run_weather_ingest.py
"""
import logging
import os
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Verify CDS API key is set
cds_key = os.environ.get("CDSAPI_KEY", "")
if not cds_key:
    logger.error("CDSAPI_KEY not set. Cannot download weather data.")
    sys.exit(1)
logger.info("CDS API key found: %s...%s", cds_key[:8], cds_key[-4:])

from datetime import date, timedelta
import duckdb

DUCKDB_CACHE_PATH = "/tmp/ingabe_cache.duckdb"

# Step 1: Check CDS API connectivity
logger.info("=== Step 1: Testing CDS API connection ===")
try:
    from src.services.weather_service import get_weather_service
    ws = get_weather_service()
    assert ws is not None and ws.is_configured(), "WeatherService not configured"
    logger.info("WeatherService is configured and ready")
except Exception as e:
    logger.error("WeatherService init failed: %s", e)
    sys.exit(1)

# Step 2: Download weather data for a recent date
target_date = (date.today() - timedelta(days=10))  # AgERA5 has ~8 day latency
logger.info("=== Step 2: Downloading AgERA5 data for %s ===", target_date)

try:
    weather_data = ws.download_agera5_day(target_date)
    if "error" in weather_data and not weather_data.get("variables"):
        logger.error("Download failed: %s", weather_data)
        sys.exit(1)

    variables = weather_data.get("variables", {})
    errors = weather_data.get("errors")
    logger.info("Downloaded %d variables: %s", len(variables), list(variables.keys()))
    if errors:
        logger.warning("Partial errors: %s", errors)
except Exception as e:
    logger.error("Download failed: %s", e)
    import traceback; traceback.print_exc()
    sys.exit(1)

# Step 3: Get district bounding boxes from PostGIS
logger.info("=== Step 3: Fetching district bounding boxes from PostGIS ===")
try:
    import psycopg2
    pg_host = os.environ.get("POSTGRES_HOST", "postgresdb")
    pg_port = os.environ.get("POSTGRES_PORT", "5432")
    pg_db = os.environ.get("POSTGRES_DB", "mundidb")
    pg_user = os.environ.get("POSTGRES_USER", "mundiuser")
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "gdalpassword")

    conn_pg = psycopg2.connect(host=pg_host, port=pg_port, database=pg_db, user=pg_user, password=pg_pass)
    cur = conn_pg.cursor()
    cur.execute("SELECT district, bbox_west, bbox_south, bbox_east, bbox_north FROM rwanda_district_boundaries ORDER BY district")
    district_rows = cur.fetchall()
    cur.close()
    conn_pg.close()

    logger.info("Found %d districts with bounding boxes", len(district_rows))

    district_geometries = [
        {"district": r[0], "bbox": (r[1], r[2], r[3], r[4])}
        for r in district_rows
    ]
except Exception as e:
    logger.error("PostGIS query failed: %s", e)
    import traceback; traceback.print_exc()
    sys.exit(1)

# Step 4: Aggregate to district level
logger.info("=== Step 4: Aggregating weather data to %d districts ===", len(district_geometries))
try:
    district_stats = ws.aggregate_to_districts(weather_data, district_geometries)
    logger.info("Produced %d district weather records", len(district_stats))

    # Show sample
    if district_stats:
        sample = district_stats[0]
        logger.info("Sample — %s: temp_mean=%.1f C, precip=%.1f mm/day, solar=%.2f MJ/m2/day",
                     sample.get("district", "?"),
                     sample.get("temperature_mean", 0),
                     sample.get("precipitation", 0),
                     sample.get("solar_radiation", 0))
except Exception as e:
    logger.error("Aggregation failed: %s", e)
    import traceback; traceback.print_exc()
    sys.exit(1)

# Step 5: Write to DuckDB cache
logger.info("=== Step 5: Writing to DuckDB cache (%s) ===", DUCKDB_CACHE_PATH)
try:
    conn = duckdb.connect(database=DUCKDB_CACHE_PATH, read_only=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS weather_daily_cache (
            district VARCHAR,
            observation_date DATE,
            temperature_mean DOUBLE,
            temperature_max DOUBLE,
            temperature_min DOUBLE,
            precipitation DOUBLE,
            solar_radiation DOUBLE,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Delete existing data for this date (idempotent)
    conn.execute("DELETE FROM weather_daily_cache WHERE observation_date = ?", [str(target_date)])

    rows_written = 0
    for stats in district_stats:
        conn.execute(
            "INSERT INTO weather_daily_cache (district, observation_date, temperature_mean, "
            "temperature_max, temperature_min, precipitation, solar_radiation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                stats.get("district"),
                stats.get("date"),
                stats.get("temperature_mean"),
                stats.get("temperature_max"),
                stats.get("temperature_min"),
                stats.get("precipitation"),
                stats.get("solar_radiation"),
            ],
        )
        rows_written += 1

    # Verify
    total = conn.execute("SELECT COUNT(*) FROM weather_daily_cache").fetchone()[0]
    conn.close()

    logger.info("Wrote %d rows for %s. Total cache rows: %d", rows_written, target_date, total)
except Exception as e:
    logger.error("DuckDB write failed: %s", e)
    import traceback; traceback.print_exc()
    sys.exit(1)

logger.info("=== Weather ingest complete! ===")
logger.info("Sage can now answer weather questions via get_weather_stats tool.")
logger.info("REST endpoint: GET /api/rwanda/weather/daily")
