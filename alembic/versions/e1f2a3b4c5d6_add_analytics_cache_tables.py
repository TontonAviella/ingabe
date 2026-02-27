"""add analytics cache tables and seed rwanda district boundaries

These cache tables were previously stored in DuckDB at
/tmp/ingabe_cache/cache.duckdb.  On Render, the Dagster daemon and web app
run in separate containers with separate ephemeral filesystems so DuckDB
cannot be shared.  Moving these tables to PostgreSQL solves the problem
because both services already connect to the same database.

This migration also seeds the ``rwanda_district_boundaries`` PostGIS table
from the geoBoundaries API if it does not already exist.

Revision ID: e1f2a3b4c5d6
Revises: c2d3e4f5a6b7
Create Date: 2026-02-27 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Analytics cache tables (migrated from DuckDB) ────────────────

    op.create_table(
        "ndvi_field_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("mean_ndvi", sa.Float),
        sa.Column("std_ndvi", sa.Float),
        sa.Column("min_ndvi", sa.Float),
        sa.Column("max_ndvi", sa.Float),
        sa.Column("valid_pixels", sa.Integer),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "agri_indices_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("admin_level", sa.String, nullable=False),
        sa.Column("admin_name", sa.String, nullable=False),
        sa.Column("parent_name", sa.String),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("ndvi_mean", sa.Float),
        sa.Column("ndvi_std", sa.Float),
        sa.Column("evi_mean", sa.Float),
        sa.Column("evi_std", sa.Float),
        sa.Column("ndwi_mean", sa.Float),
        sa.Column("ndwi_std", sa.Float),
        sa.Column("savi_mean", sa.Float),
        sa.Column("savi_std", sa.Float),
        sa.Column("ndre_mean", sa.Float),
        sa.Column("ndre_std", sa.Float),
        sa.Column("ndbi_mean", sa.Float),
        sa.Column("ndbi_std", sa.Float),
        sa.Column("valid_pixels", sa.Integer),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_agri_indices_cache_lookup",
        "agri_indices_cache",
        ["admin_level", "admin_name", "computed_at"],
    )

    op.create_table(
        "ndvi_cell_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("cell_name", sa.String, nullable=False, index=True),
        sa.Column("district_name", sa.String),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("mean_ndvi", sa.Float),
        sa.Column("std_ndvi", sa.Float),
        sa.Column("min_ndvi", sa.Float),
        sa.Column("max_ndvi", sa.Float),
        sa.Column("valid_pixels", sa.Integer),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "ndvi_parcel_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("parcel_id", sa.String, nullable=False, index=True),
        sa.Column("parcel_name", sa.String),
        sa.Column("layer_id", sa.String),
        sa.Column("week_start", sa.Date, nullable=False),
        sa.Column("mean_ndvi", sa.Float),
        sa.Column("std_ndvi", sa.Float),
        sa.Column("min_ndvi", sa.Float),
        sa.Column("max_ndvi", sa.Float),
        sa.Column("valid_pixels", sa.Integer),
        sa.Column("area_ha", sa.Float),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "weather_daily_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("observation_date", sa.Date, nullable=False),
        sa.Column("temperature_mean", sa.Float),
        sa.Column("temperature_max", sa.Float),
        sa.Column("temperature_min", sa.Float),
        sa.Column("precipitation", sa.Float),
        sa.Column("solar_radiation", sa.Float),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "ix_weather_daily_cache_lookup",
        "weather_daily_cache",
        ["district", "observation_date"],
    )

    op.create_table(
        "crop_classification_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("class_label", sa.String),
        sa.Column("area_ha", sa.Float),
        sa.Column("pixel_count", sa.Integer),
        sa.Column("confidence", sa.Float),
        sa.Column("job_id", sa.String),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "anomaly_alerts_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("h3_index", sa.String),
        sa.Column("parcel_id", sa.String),
        sa.Column("anomaly_date", sa.Date),
        sa.Column("observed_ndvi", sa.Float),
        sa.Column("expected_ndvi", sa.Float),
        sa.Column("z_score", sa.Float),
        sa.Column("severity", sa.String),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "yield_risk_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("risk_level", sa.String),
        sa.Column("risk_description", sa.String),
        sa.Column("trend_slope", sa.Float),
        sa.Column("kendall_tau", sa.Float),
        sa.Column("latest_ndvi", sa.Float),
        sa.Column("mean_ndvi", sa.Float),
        sa.Column("seasonal_deviation", sa.Float),
        sa.Column("observations", sa.Integer),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "drought_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("drought_status", sa.String),
        sa.Column("current_vci", sa.Float),
        sa.Column("latest_ndvi", sa.Float),
        sa.Column("latest_ndwi", sa.Float),
        sa.Column("drought_period_count", sa.Integer),
        sa.Column("description", sa.Text),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "phenology_cache",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("district", sa.String, nullable=False, index=True),
        sa.Column("current_stage", sa.String),
        sa.Column("peak_ndvi", sa.Float),
        sa.Column("peak_date", sa.String),
        sa.Column("green_up_start", sa.String),
        sa.Column("senescence_start", sa.String),
        sa.Column("harvest_date", sa.String),
        sa.Column("observations", sa.Integer),
        sa.Column(
            "computed_at", sa.DateTime, server_default=sa.text("NOW()"),
        ),
    )

    # ── 2. Seed rwanda_district_boundaries from geoBoundaries API ───────
    _seed_rwanda_districts()


def _seed_rwanda_districts() -> None:
    """Fetch Rwanda ADM2 boundaries from geoBoundaries and insert them.

    Idempotent — skips if the table already has >= 30 rows.
    """
    import json
    import logging

    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    # Ensure PostGIS is available (entrypoint.sh already creates it but
    # just in case this migration runs before the entrypoint step)
    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))

    # Create the table if it doesn't exist (Dagster asset normally does
    # this, but we want the migration to be self-contained)
    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS rwanda_district_boundaries (
            district VARCHAR PRIMARY KEY,
            geom GEOMETRY(MultiPolygon, 4326),
            bbox_west DOUBLE PRECISION,
            bbox_south DOUBLE PRECISION,
            bbox_east DOUBLE PRECISION,
            bbox_north DOUBLE PRECISION
        )
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_districts_geom
        ON rwanda_district_boundaries USING GIST (geom)
    """))

    # Check if already populated
    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_district_boundaries")
    )
    count = result.scalar()
    if count and count >= 30:
        logger.info(
            "rwanda_district_boundaries already has %d rows — skipping seed",
            count,
        )
        return

    # Fetch from geoBoundaries API
    api_url = (
        "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM2/"
    )
    try:
        api_resp = requests.get(api_url, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            logger.warning("No gjDownloadURL in geoBoundaries API response")
            return

        geojson_resp = requests.get(geojson_url, timeout=120)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as exc:
        logger.warning(
            "Failed to fetch geoBoundaries data (non-fatal): %s", exc,
        )
        return

    # Clear any partial data and insert fresh
    conn.execute(sa.text("DELETE FROM rwanda_district_boundaries"))

    loaded = 0
    for feat in features:
        name = feat.get("properties", {}).get("shapeName")
        if not name:
            continue
        geom_json = json.dumps(feat["geometry"])
        conn.execute(
            sa.text("""
                INSERT INTO rwanda_district_boundaries
                    (district, geom, bbox_west, bbox_south, bbox_east,
                     bbox_north)
                VALUES (
                    :name,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)),
                    ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(:geom)))
                )
            """),
            {"name": name, "geom": geom_json},
        )
        loaded += 1

    logger.info("Seeded %d Rwanda district boundaries", loaded)


def downgrade() -> None:
    op.drop_table("phenology_cache")
    op.drop_table("drought_cache")
    op.drop_table("yield_risk_cache")
    op.drop_table("anomaly_alerts_cache")
    op.drop_table("crop_classification_cache")
    op.drop_table("weather_daily_cache")
    op.drop_table("ndvi_parcel_cache")
    op.drop_table("ndvi_cell_cache")
    op.drop_table("agri_indices_cache")
    op.drop_table("ndvi_field_cache")
    # Note: rwanda_district_boundaries is NOT dropped — it belongs to the
    # Dagster asset and may have been created independently.
