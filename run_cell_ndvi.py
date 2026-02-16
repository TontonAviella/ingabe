#!/usr/bin/env python3
"""Standalone runner: time the full 2,148-cell NDVI pipeline.

Usage (inside Docker):
    docker compose exec app python run_cell_ndvi.py
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta

import duckdb
import numpy as np
import psycopg2

# ── config ──────────────────────────────────────────────────────────
PG_DSN = dict(
    host=os.environ.get("POSTGRES_HOST", "postgresdb"),
    port=int(os.environ.get("POSTGRES_PORT", "5432")),
    database=os.environ.get("POSTGRES_DB", "mundidb"),
    user=os.environ.get("POSTGRES_USER", "mundiuser"),
    password=os.environ.get("POSTGRES_PASSWORD", "gdalpassword"),
)
DUCKDB_PATH = os.environ.get("DUCKDB_PATH", "/tmp/ingabe_cache.duckdb")

# ── helpers ─────────────────────────────────────────────────────────

def _ensure_cache_tables(conn):
    """Create DuckDB cache tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ndvi_cell_cache (
            cell_name VARCHAR, district_name VARCHAR, week_start DATE,
            mean_ndvi DOUBLE, std_ndvi DOUBLE, min_ndvi DOUBLE,
            max_ndvi DOUBLE, valid_pixels INTEGER,
            computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)


def main():
    from src.services.sentinel_hub_service import get_sentinel_hub_service

    sh = get_sentinel_hub_service()
    if sh is None or not sh.is_configured():
        print("ERROR: Sentinel Hub not configured", file=sys.stderr)
        sys.exit(1)

    # Fetch all cells from PostGIS
    pg = psycopg2.connect(**PG_DSN)
    cur = pg.cursor()
    cur.execute("""
        SELECT cell_id, cell_name, district_name, ST_AsGeoJSON(geom)
        FROM rwanda_cell_boundaries
        ORDER BY cell_id
    """)
    cell_rows = cur.fetchall()
    cur.close()
    pg.close()

    total = len(cell_rows)
    print(f"Starting full cell NDVI run: {total} cells")
    print(f"Start time: {datetime.utcnow().isoformat()}Z")

    now = datetime.utcnow()
    date_from = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    date_to = now.strftime("%Y-%m-%d")
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")

    duck = duckdb.connect(database=DUCKDB_PATH, read_only=False)
    _ensure_cache_tables(duck)

    # Clear old data for this week to avoid duplicates
    duck.execute("DELETE FROM ndvi_cell_cache WHERE week_start = ?", [week_start])
    duck.close()

    t0 = time.time()
    rows_written = 0
    errors = 0
    skipped = 0

    for i, (cell_id, cell_name, district_name, geom_geojson) in enumerate(cell_rows):
        try:
            if not geom_geojson:
                skipped += 1
                continue

            geometry = json.loads(geom_geojson)
            stats = sh.get_field_stats(
                geometry=geometry,
                date_from=date_from,
                date_to=date_to,
                index="ndvi",
            )

            if "error" in stats:
                errors += 1
                if errors <= 10:
                    print(f"  SH error cell #{i} ({cell_name}): {stats['error']}")
                continue

            intervals = stats.get("intervals", [])
            if not intervals:
                skipped += 1
                continue

            ndvi_means = [
                iv["ndvi"]["mean"]
                for iv in intervals
                if "ndvi" in iv
                and iv["ndvi"].get("valid_pixels", 0) > 0
                and not math.isnan(iv["ndvi"]["mean"])
            ]
            if not ndvi_means:
                skipped += 1
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

            duck = duckdb.connect(database=DUCKDB_PATH, read_only=False)
            _ensure_cache_tables(duck)
            duck.execute(
                """
                INSERT INTO ndvi_cell_cache
                    (cell_name, district_name, week_start,
                     mean_ndvi, std_ndvi, min_ndvi, max_ndvi, valid_pixels)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [cell_name, district_name, week_start,
                 mean_ndvi, std_ndvi, min_ndvi, max_ndvi, total_pixels],
            )
            duck.close()

            rows_written += 1

        except Exception as e:
            errors += 1
            if errors <= 10:
                print(f"  Exception cell #{i} ({cell_name}): {e}")

        # Progress every 50 cells
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(
                f"  [{i+1}/{total}] "
                f"written={rows_written} err={errors} skip={skipped} "
                f"elapsed={elapsed:.0f}s rate={rate:.2f}cells/s "
                f"ETA={eta/60:.1f}min"
            )

    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"DONE: {total} cells in {elapsed:.1f}s ({elapsed/60:.1f} min)")
    print(f"  Written: {rows_written}")
    print(f"  Errors:  {errors}")
    print(f"  Skipped: {skipped}")
    print(f"  Rate:    {total/elapsed:.2f} cells/s ({elapsed/total:.2f} s/cell)")
    print(f"End time: {datetime.utcnow().isoformat()}Z")


if __name__ == "__main__":
    main()
