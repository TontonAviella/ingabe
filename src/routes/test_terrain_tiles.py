"""3D terrain tiles: fetched once from the source, then served from our store; only Rwanda's region is kept."""
import math
import uuid

import pytest

from src.routes import basemap_routes

PNG = b"\x89PNG\r\n\x1a\n" + uuid.uuid4().bytes  # unique per run, so a kept tile from an earlier run cannot pass


def _tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    n = 2**z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


def test_tiles_over_rwanda_are_kept_and_elsewhere_are_not():
    assert basemap_routes.keeps_terrain_tile(12, *_tile(30.06, -1.95, 12))  # Kigali
    assert basemap_routes.keeps_terrain_tile(15, *_tile(29.74, -2.60, 15))  # Huye
    assert not basemap_routes.keeps_terrain_tile(12, *_tile(2.35, 48.85, 12))  # Paris


@pytest.mark.anyio
async def test_a_tile_is_fetched_once_then_served_from_our_store(client, monkeypatch):
    calls = []

    async def from_source(z, x, y):
        calls.append((z, x, y))
        return PNG

    monkeypatch.setattr(basemap_routes, "fetch_terrain_tile", from_source)
    # A random deep tile inside the kept region, so no earlier run has kept it.
    z = 15
    x, y = _tile(29.0 + uuid.uuid4().int % 1000 / 400, -1.2 - uuid.uuid4().int % 1000 / 500, z)
    for _ in range(2):
        response = await client.get(f"/api/basemaps/terrain/{z}/{x}/{y}.png")
        assert response.status_code == 200
        assert response.content == PNG
        assert "immutable" in response.headers["cache-control"]
    assert len(calls) == 1


@pytest.mark.anyio
async def test_impossible_tiles_are_refused(client):
    assert (await client.get("/api/basemaps/terrain/16/0/0.png")).status_code == 404
    assert (await client.get("/api/basemaps/terrain/2/4/0.png")).status_code == 404
