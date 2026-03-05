"""fix rwanda admin boundary district_name and re-seed empty tables

Two issues fixed:

1. **Wrong district_name in sectors**: The original seed used geoBoundaries
   ``shapeGroup`` which returns "RWA" (country code) for ADM3, not the actual
   district name. The back-fill had a restrictive ``AND district_name = ''``
   condition that never triggered since "RWA" is non-empty. This migration
   does an unconditional spatial join to fix district_name for sectors.

2. **Empty tables**: Previous seed migrations swallowed API download failures.
   If any table is under-populated, this migration re-downloads from
   geoBoundaries and RAISES on failure so Alembic retries on next deploy.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-03-05 08:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _reseed_districts()
    _reseed_sectors()
    _reseed_cells()
    _reseed_villages()
    # Always fix district_name via unconditional spatial join — even when
    # tables were already populated but had wrong values from shapeGroup.
    _fix_district_names()


# ---------------------------------------------------------------------------
# Districts (ADM2, ~30 features)
# ---------------------------------------------------------------------------

def _reseed_districts() -> None:
    import json
    import logging
    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS postgis"))
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

    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_district_boundaries")
    ).scalar()
    if count and count >= 30:
        logger.info("rwanda_district_boundaries already has %d rows — skip", count)
        return

    logger.info("rwanda_district_boundaries has %d rows — re-seeding", count or 0)

    api_url = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM2/"
    # RAISE on failure — migration stays unapplied and retries next deploy
    api_resp = requests.get(api_url, timeout=30)
    api_resp.raise_for_status()
    geojson_url = api_resp.json().get("gjDownloadURL")
    if not geojson_url:
        raise RuntimeError("No gjDownloadURL in geoBoundaries ADM2 response")

    geojson_resp = requests.get(geojson_url, timeout=120)
    geojson_resp.raise_for_status()
    features = geojson_resp.json().get("features", [])
    if not features:
        raise RuntimeError("geoBoundaries ADM2 returned 0 features")

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
                    (district, geom, bbox_west, bbox_south, bbox_east, bbox_north)
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

    conn.execute(sa.text("""
        CREATE INDEX IF NOT EXISTS idx_rwanda_districts_geom
        ON rwanda_district_boundaries USING GIST (geom)
    """))

    logger.info("Re-seeded %d Rwanda district boundaries", loaded)


# ---------------------------------------------------------------------------
# Sectors (ADM3, ~416 features)
# ---------------------------------------------------------------------------

def _reseed_sectors() -> None:
    import json
    import logging
    import requests

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

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

    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_sector_boundaries")
    ).scalar()
    if count and count >= 400:
        logger.info("rwanda_sector_boundaries already has %d rows — skip", count)
        return

    logger.info("rwanda_sector_boundaries has %d rows — re-seeding", count or 0)

    api_url = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM3/"
    api_resp = requests.get(api_url, timeout=30)
    api_resp.raise_for_status()
    geojson_url = api_resp.json().get("gjDownloadURL")
    if not geojson_url:
        raise RuntimeError("No gjDownloadURL in geoBoundaries ADM3 response")

    geojson_resp = requests.get(geojson_url, timeout=180)
    geojson_resp.raise_for_status()
    features = geojson_resp.json().get("features", [])
    if not features:
        raise RuntimeError("geoBoundaries ADM3 returned 0 features")

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

    # Back-fill district_name via spatial join
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

    logger.info("Re-seeded %d Rwanda sector boundaries", loaded)


# ---------------------------------------------------------------------------
# Cells (ADM4, ~2,148 features)
# ---------------------------------------------------------------------------

def _reseed_cells() -> None:
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

    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_cell_boundaries")
    ).scalar()
    if count and count >= 2000:
        logger.info("rwanda_cell_boundaries already has %d rows — skip", count)
        return

    logger.info("rwanda_cell_boundaries has %d rows — re-seeding", count or 0)

    api_url = "https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM4/"
    api_resp = requests.get(api_url, timeout=30)
    api_resp.raise_for_status()
    geojson_url = api_resp.json().get("gjDownloadURL")
    if not geojson_url:
        raise RuntimeError("No gjDownloadURL in geoBoundaries ADM4 response")

    geojson_resp = requests.get(geojson_url, timeout=300)
    geojson_resp.raise_for_status()
    features = geojson_resp.json().get("features", [])
    if not features:
        raise RuntimeError("geoBoundaries ADM4 returned 0 features")

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

    # Back-fill sector_name from spatial join
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

    logger.info("Re-seeded %d Rwanda cell boundaries", loaded)


# ---------------------------------------------------------------------------
# Villages (ADM5, ~14,815 features — simplified geometry)
# ---------------------------------------------------------------------------

def _reseed_villages() -> None:
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

    count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_village_boundaries")
    ).scalar()
    if count and count >= 14000:
        logger.info("rwanda_village_boundaries already has %d rows — skip", count)
        return

    logger.info("rwanda_village_boundaries has %d rows — re-seeding", count or 0)

    simplified_url = (
        "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
        "releaseData/gbOpen/RWA/ADM5/"
        "geoBoundaries-RWA-ADM5_simplified.geojson"
    )
    geojson_resp = requests.get(simplified_url, timeout=300)
    geojson_resp.raise_for_status()
    data = geojson_resp.json()
    features = data.get("features", [])
    del data
    if not features:
        raise RuntimeError("geoBoundaries ADM5 simplified returned 0 features")

    conn.execute(sa.text("DELETE FROM rwanda_village_boundaries"))

    loaded = 0
    for feat in features:
        props = feat.get("properties", {})
        village_name = props.get("shapeName", "")
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
        if loaded % 500 == 0:
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

    # Back-fill cell_name from spatial join
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

    logger.info("Re-seeded %d Rwanda village boundaries", loaded)


# ---------------------------------------------------------------------------
# Fix district_name (unconditional spatial join)
# ---------------------------------------------------------------------------

def _fix_district_names() -> None:
    """Unconditionally back-fill district_name in sectors, cells, and villages.

    The original seeds used geoBoundaries shapeGroup which often contains
    the country code "RWA" instead of the actual district name.  The original
    back-fill only ran when district_name was NULL or empty, so "RWA" values
    were never corrected.  This does an unconditional spatial join.
    """
    import logging

    logger = logging.getLogger(__name__)
    conn = op.get_bind()

    # Check that districts table has data (needed for spatial join)
    district_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_district_boundaries")
    ).scalar()
    if not district_count or district_count < 1:
        logger.warning("rwanda_district_boundaries is empty — cannot fix district_name")
        return

    # Sectors: unconditional — overwrite "RWA" with actual district name
    try:
        result = conn.execute(sa.text("""
            UPDATE rwanda_sector_boundaries s
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(s.geom), d.geom)
        """))
        logger.info("Fixed sector district_name via spatial join (%d rows)", result.rowcount)
    except Exception as e:
        logger.warning("Could not fix sector district_name: %s", e)

    # Cells: unconditional
    try:
        result = conn.execute(sa.text("""
            UPDATE rwanda_cell_boundaries c
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(c.geom), d.geom)
        """))
        logger.info("Fixed cell district_name via spatial join (%d rows)", result.rowcount)
    except Exception as e:
        logger.warning("Could not fix cell district_name: %s", e)

    # Villages: unconditional
    try:
        result = conn.execute(sa.text("""
            UPDATE rwanda_village_boundaries v
            SET district_name = d.district
            FROM rwanda_district_boundaries d
            WHERE ST_Within(ST_Centroid(v.geom), d.geom)
        """))
        logger.info("Fixed village district_name via spatial join (%d rows)", result.rowcount)
    except Exception as e:
        logger.warning("Could not fix village district_name: %s", e)

    # Also fix sector_name for cells and villages
    sector_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_sector_boundaries")
    ).scalar()
    if sector_count and sector_count > 0:
        try:
            conn.execute(sa.text("""
                UPDATE rwanda_cell_boundaries c
                SET sector_name = s.sector_name
                FROM rwanda_sector_boundaries s
                WHERE ST_Within(ST_Centroid(c.geom), s.geom)
            """))
            logger.info("Fixed cell sector_name via spatial join")
        except Exception as e:
            logger.warning("Could not fix cell sector_name: %s", e)

        try:
            conn.execute(sa.text("""
                UPDATE rwanda_village_boundaries v
                SET sector_name = s.sector_name
                FROM rwanda_sector_boundaries s
                WHERE ST_Within(ST_Centroid(v.geom), s.geom)
            """))
            logger.info("Fixed village sector_name via spatial join")
        except Exception as e:
            logger.warning("Could not fix village sector_name: %s", e)

    # Fix cell_name for villages
    cell_count = conn.execute(
        sa.text("SELECT COUNT(*) FROM rwanda_cell_boundaries")
    ).scalar()
    if cell_count and cell_count > 0:
        try:
            conn.execute(sa.text("""
                UPDATE rwanda_village_boundaries v
                SET cell_name = c.cell_name
                FROM rwanda_cell_boundaries c
                WHERE ST_Within(ST_Centroid(v.geom), c.geom)
            """))
            logger.info("Fixed village cell_name via spatial join")
        except Exception as e:
            logger.warning("Could not fix village cell_name: %s", e)


def downgrade() -> None:
    # Data-only migration — downgrade is a no-op (tables remain)
    pass
