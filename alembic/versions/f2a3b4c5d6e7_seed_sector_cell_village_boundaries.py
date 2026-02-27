"""seed rwanda sector, cell, and village boundaries from geoBoundaries

Seeds three admin boundary tables from the geoBoundaries public API:
- ``rwanda_sector_boundaries`` (~416 ADM3 features)
- ``rwanda_cell_boundaries`` (~2,148 ADM4 features)
- ``rwanda_village_boundaries`` (~14,815 ADM5 features — simplified geometry)

Follows the same pattern as the district seed in migration e1f2a3b4c5d6.
All tables are idempotent — they skip if already populated.

Memory: ADM5 uses the simplified GeoJSON (~11 MB) instead of the full
version (~130 MB) to stay within Render's 512 MB container limit.
Features are iterated one at a time.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-02-27 18:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _seed_rwanda_sectors()
    _seed_rwanda_cells()
    _seed_rwanda_villages()


# ── Sector boundaries (ADM3, ~416 features) ──────────────────────────────


def _seed_rwanda_sectors() -> None:
    """Fetch Rwanda ADM3 sector boundaries from geoBoundaries and insert."""
    import json
    import logging

    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))

    conn.execute(sa.text("""
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
    """))

    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_sector_boundaries")
    )
    count = result.scalar()
    if count and count >= 400:
        logger.info(
            "rwanda_sector_boundaries already has %d rows - skipping", count,
        )
        return

    api_url = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM3/"
    try:
        api_resp = requests.get(api_url, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            logger.warning("No gjDownloadURL in geoBoundaries ADM3 response")
            return

        geojson_resp = requests.get(geojson_url, timeout=180)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as exc:
        logger.warning("Failed to fetch ADM3 boundaries (non-fatal): %s", exc)
        return

    logger.info("Downloaded %d sector features", len(features))

    conn.execute(sa.text("DELETE FROM rwanda_sector_boundaries"))

    loaded = 0
    for feat in features:
        props = feat.get("properties", {})
        sector_name = props.get("shapeName", "")
        district_name = props.get("shapeGroup", "")
        if not sector_name:
            continue

        geom_json = json.dumps(feat["geometry"])
        conn.execute(
            sa.text("""
                INSERT INTO rwanda_sector_boundaries
                    (sector_name, district_name, geom, area_km2,
                     bbox_west, bbox_south, bbox_east, bbox_north)
                VALUES (
                    :sector_name, :district_name,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)),
                    ST_Area(ST_Transform(
                        ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326), 32736
                    )) / 1e6,
                    ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(:geom)))
                )
            """),
            {"sector_name": sector_name, "district_name": district_name, "geom": geom_json},
        )
        loaded += 1

    # Free memory before creating indexes
    del features

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_geom
        ON rwanda_sector_boundaries USING GIST (geom)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_district
        ON rwanda_sector_boundaries (LOWER(district_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_sectors_name
        ON rwanda_sector_boundaries (LOWER(sector_name))
    """))

    # Back-fill district_name via spatial join if geoBoundaries left it blank
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_sector_boundaries s
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(s.geom), d.geom)
              AND (s.district_name IS NULL OR s.district_name = '')
        """))
    except Exception as e:
        logger.warning("Could not back-fill sector district_name: %s", e)

    logger.info("Seeded %d Rwanda sector boundaries", loaded)


# ── Cell boundaries (ADM4, ~2,148 features) ──────────────────────────────


def _seed_rwanda_cells() -> None:
    """Fetch Rwanda ADM4 cell boundaries from geoBoundaries and insert."""
    import json
    import logging

    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    conn.execute(sa.text("""
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
    """))

    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_cell_boundaries")
    )
    count = result.scalar()
    if count and count >= 2000:
        logger.info(
            "rwanda_cell_boundaries already has %d rows - skipping", count,
        )
        return

    api_url = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM4/"
    try:
        api_resp = requests.get(api_url, timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            logger.warning("No gjDownloadURL in geoBoundaries ADM4 response")
            return

        geojson_resp = requests.get(geojson_url, timeout=300)
        geojson_resp.raise_for_status()
        features = geojson_resp.json().get("features", [])
    except Exception as exc:
        logger.warning("Failed to fetch ADM4 boundaries (non-fatal): %s", exc)
        return

    logger.info("Downloaded %d cell features", len(features))

    conn.execute(sa.text("DELETE FROM rwanda_cell_boundaries"))

    loaded = 0
    for feat in features:
        props = feat.get("properties", {})
        cell_name = props.get("shapeName", "")
        sector_name = props.get("shapeGroup", "")
        if not cell_name:
            continue

        geom_json = json.dumps(feat["geometry"])
        conn.execute(
            sa.text("""
                INSERT INTO rwanda_cell_boundaries
                    (cell_name, sector_name, district_name, geom, area_km2,
                     bbox_west, bbox_south, bbox_east, bbox_north)
                VALUES (
                    :cell_name, :sector_name, NULL,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)),
                    ST_Area(ST_Transform(
                        ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326), 32736
                    )) / 1e6,
                    ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(:geom)))
                )
            """),
            {"cell_name": cell_name, "sector_name": sector_name, "geom": geom_json},
        )
        loaded += 1

    del features

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_cells_geom
        ON rwanda_cell_boundaries USING GIST (geom)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_cells_sector
        ON rwanda_cell_boundaries (LOWER(sector_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_cells_name
        ON rwanda_cell_boundaries (LOWER(cell_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_cells_district
        ON rwanda_cell_boundaries (LOWER(district_name))
    """))

    # Back-fill district_name from spatial join
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_cell_boundaries c
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(c.geom), d.geom)
        """))
        logger.info("Back-filled cell district_name via spatial join")
    except Exception as e:
        logger.warning("Could not back-fill cell district_name: %s", e)

    # Back-fill sector_name from spatial join (if geoBoundaries shapeGroup was wrong)
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_cell_boundaries c
            SET sector_name = s.sector_name
            FROM rwanda_sector_boundaries s
            WHERE ST_Within(ST_Centroid(c.geom), s.geom)
              AND (c.sector_name IS NULL OR c.sector_name = '')
        """))
    except Exception as e:
        logger.warning("Could not back-fill cell sector_name: %s", e)

    logger.info("Seeded %d Rwanda cell boundaries", loaded)


# ── Village boundaries (ADM5, ~14,815 features — simplified) ─────────────


def _seed_rwanda_villages() -> None:
    """Fetch Rwanda ADM5 village boundaries from geoBoundaries and insert.

    Uses the simplified GeoJSON (~11 MB) instead of the full version
    (~130 MB) to stay within the 512 MB container memory limit.
    """
    import json
    import logging

    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    conn.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS rwanda_village_boundaries (
            village_id SERIAL PRIMARY KEY,
            village_name VARCHAR,
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
    """))

    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_village_boundaries")
    )
    count = result.scalar()
    if count and count >= 14000:
        logger.info(
            "rwanda_village_boundaries already has %d rows - skipping", count,
        )
        return

    # Use the SIMPLIFIED GeoJSON to stay within memory limits
    # Full: 130 MB, Simplified: 11 MB
    simplified_url = (
        "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
        "releaseData/gbOpen/RWA/ADM5/"
        "geoBoundaries-RWA-ADM5_simplified.geojson"
    )
    try:
        geojson_resp = requests.get(simplified_url, timeout=300)
        geojson_resp.raise_for_status()
        data = geojson_resp.json()
        features = data.get("features", [])
        # Free the raw response immediately
        del data
    except Exception as exc:
        logger.warning(
            "Failed to fetch ADM5 simplified boundaries (non-fatal): %s", exc,
        )
        return

    logger.info("Downloaded %d village features (simplified)", len(features))

    conn.execute(sa.text("DELETE FROM rwanda_village_boundaries"))

    loaded = 0
    batch_size = 500
    for i, feat in enumerate(features):
        props = feat.get("properties", {})
        village_name = props.get("shapeName", "")
        # ADM5 shapeGroup is the parent admin level (cell)
        cell_name = props.get("shapeGroup", "")
        if not village_name:
            continue

        geom_json = json.dumps(feat["geometry"])
        conn.execute(
            sa.text("""
                INSERT INTO rwanda_village_boundaries
                    (village_name, cell_name, sector_name, district_name,
                     geom, area_km2,
                     bbox_west, bbox_south, bbox_east, bbox_north)
                VALUES (
                    :village_name, :cell_name, NULL, NULL,
                    ST_Multi(ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)),
                    ST_Area(ST_Transform(
                        ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326), 32736
                    )) / 1e6,
                    ST_XMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMin(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_XMax(ST_Envelope(ST_GeomFromGeoJSON(:geom))),
                    ST_YMax(ST_Envelope(ST_GeomFromGeoJSON(:geom)))
                )
            """),
            {"village_name": village_name, "cell_name": cell_name, "geom": geom_json},
        )
        loaded += 1

        # Log progress every 500 features
        if loaded % batch_size == 0:
            logger.info("Inserted %d / %d village boundaries...", loaded, len(features))

    del features

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_villages_geom
        ON rwanda_village_boundaries USING GIST (geom)
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_villages_name
        ON rwanda_village_boundaries (LOWER(village_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_villages_cell
        ON rwanda_village_boundaries (LOWER(cell_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_villages_sector
        ON rwanda_village_boundaries (LOWER(sector_name))
    """))
    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_villages_district
        ON rwanda_village_boundaries (LOWER(district_name))
    """))

    # Back-fill district_name from spatial join
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_village_boundaries v
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(v.geom), d.geom)
        """))
        logger.info("Back-filled village district_name via spatial join")
    except Exception as e:
        logger.warning("Could not back-fill village district_name: %s", e)

    # Back-fill sector_name from spatial join
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_village_boundaries v
            SET sector_name = s.sector_name
            FROM rwanda_sector_boundaries s
            WHERE ST_Within(ST_Centroid(v.geom), s.geom)
        """))
        logger.info("Back-filled village sector_name via spatial join")
    except Exception as e:
        logger.warning("Could not back-fill village sector_name: %s", e)

    # Back-fill cell_name from spatial join (if shapeGroup was wrong)
    try:
        conn.execute(sa.text("""
            UPDATE rwanda_village_boundaries v
            SET cell_name = c.cell_name
            FROM rwanda_cell_boundaries c
            WHERE ST_Within(ST_Centroid(v.geom), c.geom)
              AND (v.cell_name IS NULL OR v.cell_name = '')
        """))
    except Exception as e:
        logger.warning("Could not back-fill village cell_name: %s", e)

    logger.info("Seeded %d Rwanda village boundaries", loaded)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS rwanda_village_boundaries CASCADE")
    op.execute("DROP TABLE IF EXISTS rwanda_cell_boundaries CASCADE")
    op.execute("DROP TABLE IF EXISTS rwanda_sector_boundaries CASCADE")
