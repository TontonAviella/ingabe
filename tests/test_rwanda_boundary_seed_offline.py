"""The Rwanda boundary seed migrations must not need the network.

CI failed on 2026-10-04 because migration b2c3d4e5f6a7 downloaded ADM2
districts from geoboundaries.org at migration time and the API timed out
(`requests.exceptions.ConnectTimeout`, then "Migration failed" and a pytest
INTERNALERROR). The features now come from files vendored under
src/database/seed_data/geoboundaries/. These tests load every level and
run the b2c3d4e5f6a7 district and sector re-seed on empty tables with every
network connection made from Python failing, and check that no seed
migration imports an HTTP client.

The re-seed runs in a scratch schema inside a transaction that is rolled
back, so it never touches the real boundary tables. It stops at sectors:
the full re-seed (cells, villages) takes minutes and uses the same loader.
"""

import importlib.util
import socket
import uuid
from pathlib import Path

import pytest
import requests
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from src.database.geoboundaries import rwanda_boundary_features
from src.database.pool import _build_postgres_url
from src.services.admin_boundaries import RWANDA_DISTRICTS

_VERSIONS = Path(__file__).resolve().parent.parent / "alembic" / "versions"
_SEED_MIGRATIONS = (
    _VERSIONS / "e1f2a3b4c5d6_add_analytics_cache_tables.py",
    _VERSIONS / "f2a3b4c5d6e7_seed_sector_cell_village_boundaries.py",
    _VERSIONS / "b2c3d4e5f6a7_reseed_empty_rwanda_boundaries.py",
)

# Unit counts published by geoBoundaries for gbOpen RWA (admUnitCount).
_UNIT_COUNTS = {"ADM2": 30, "ADM3": 416, "ADM4": 2148, "ADM5": 14815}


@pytest.fixture
def no_network(monkeypatch):
    """Fail every DNS lookup and socket connect made from Python.

    psycopg2 connects through libpq (C), so the database stays reachable.
    """

    def refuse(*args, **kwargs):
        raise OSError("network disabled by test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("https://www.geoboundaries.org/api/current/gbOpen/RWA/ADM2/", timeout=5)


@pytest.mark.parametrize("level", sorted(_UNIT_COUNTS))
def test_features_load_without_network(no_network, level):
    features = rwanda_boundary_features(level)

    assert len(features) == _UNIT_COUNTS[level]
    assert all(f["properties"]["shapeName"] for f in features)
    assert all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in features)


def test_districts_match_official_names(no_network):
    names = {f["properties"]["shapeName"] for f in rwanda_boundary_features("ADM2")}

    assert names == set(RWANDA_DISTRICTS)


@pytest.mark.parametrize("path", _SEED_MIGRATIONS, ids=lambda p: p.name[:12])
def test_seed_migrations_import_no_http_client(path):
    source = path.read_text()

    for client in ("import requests", "import httpx", "urllib.request"):
        assert client not in source


def _load_reseed_migration():
    spec = importlib.util.spec_from_file_location("reseed_b2c3d4e5f6a7", _SEED_MIGRATIONS[2])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ~20 s on a laptop; headroom over the 60 s default for a busy CI worker.
@pytest.mark.timeout(180)
def test_reseed_fills_empty_district_and_sector_tables_without_network(no_network):
    migration = _load_reseed_migration()
    schema = f"gb_seed_test_{uuid.uuid4().hex[:8]}"
    engine = sa.create_engine(_build_postgres_url(), poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as conn:
            tx = conn.begin()
            try:
                conn.execute(sa.text(f"CREATE SCHEMA {schema}"))
                conn.execute(sa.text(f"SET LOCAL search_path TO {schema}, public"))
                with Operations.context(MigrationContext.configure(conn)):
                    migration._reseed_districts()
                    migration._reseed_sectors()

                counts = {
                    table: conn.execute(sa.text(f"SELECT COUNT(*) FROM {schema}.{table}")).scalar()
                    for table in ("rwanda_district_boundaries", "rwanda_sector_boundaries")
                }
                # The district_name fix (_fix_district_names) joins each
                # sector's centroid to a district, so every one must land.
                orphan_sectors = conn.execute(
                    sa.text(f"""
                        SELECT COUNT(*) FROM {schema}.rwanda_sector_boundaries s
                        WHERE NOT EXISTS (
                            SELECT 1 FROM {schema}.rwanda_district_boundaries d
                            WHERE ST_Within(ST_Centroid(s.geom), d.geom)
                        )
                    """)
                ).scalar()
            finally:
                tx.rollback()
    finally:
        engine.dispose()

    assert counts == {"rwanda_district_boundaries": 30, "rwanda_sector_boundaries": 416}
    assert orphan_sectors == 0
