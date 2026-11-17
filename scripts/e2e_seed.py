#!/usr/bin/env python3
"""The farm the browser tests open (frontendts/e2e/field-flights.spec.ts): one project, two flights of the same
ground, one kept field.

Run inside the app container the browser tests talk to, never by hand against real data:

    docker exec mundi-app-e2e python scripts/e2e_seed.py      (locally, see scripts/e2e_app.sh)
    docker compose exec -T app python scripts/e2e_seed.py     (CI)

It refuses the live database (mundidb) and an app with a sign-in provider: the project belongs to the shared
no-sign-in user, who exists only where nobody signs in. Running it twice gives the same farm (its rows and files
have fixed names and are replaced).

The two photos are small made-up multispectral pictures whose every pixel is the same, so the right answer is
known beforehand: red 0.05 and near-infrared 0.45 give a health index of 0.80 (April), red 0.10 and 0.30 give
0.50 (March). They are whole numbers with the scale 0.0001 written in the file, the one scale the app trusts
(src/services/photo_bands.py), so a number may be said.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import asyncpg
import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database.pool import _build_postgres_url  # noqa: E402  (the seeding connection the route tests use too)
from src.dependencies.session import LegacyUserContext  # noqa: E402
from src.services import field_taps  # noqa: E402
from src.utils import get_async_s3_client, get_bucket_name  # noqa: E402

LIVE_DATABASE = "mundidb"
PROJECT, MAP = "PE2EFLIGHTS1", "ME2EFLIGHTS1"
FIELD_ID = "e2efield0001"
PIXEL_M, SIDE_PX = 0.5, 256  # each picture is 128 m across
FIELD_HALF_M = 30.0  # the kept field: 60 m by 60 m in the middle of the picture
CENTRE_LON, CENTRE_LAT = 30.0600, -1.9500
BAND_NAMES = ("Blue", "Green", "Red", "RedEdge", "NIR")
# layer id, name, flight day, reflectance of (blue, green, red, red edge, near-infrared) times 10,000
FLIGHTS = (("LE2EAPRIL001", "April flight", "2026-04-02", (300, 600, 500, 2000, 4500)),
           ("LE2EMARCH001", "March flight", "2026-03-14", (300, 600, 1000, 2000, 3000)))
EXPECTED = {"project": PROJECT, "map": MAP, "field": 1,
            "flights": [{"day_said": "14 March 2026", "said": "health index (NDVI) 0.50"},
                        {"day_said": "2 April 2026", "said": "health index (NDVI) 0.80"}]}


def refuse_real_data() -> None:
    database = os.environ.get("POSTGRES_DB", "")
    if database in ("", LIVE_DATABASE):
        sys.exit(f"POSTGRES_DB={database or '(unset)'} is the live database: the test farm is not seeded there.")
    if os.environ.get("AUTH_PROVIDER", "").strip():
        sys.exit("This app has a sign-in provider: the test farm is only for an app nobody signs in to.")


def key_of(layer_id: str) -> str:
    return f"e2e/field_flights/{layer_id}.tif"


def write_photo(path: str, west: float, north: float, values: tuple[int, ...]) -> None:
    profile = {"driver": "COG", "width": SIDE_PX, "height": SIDE_PX, "count": len(values), "dtype": "uint16",
               "crs": "EPSG:3857", "transform": from_origin(west, north, PIXEL_M, PIXEL_M), "compress": "deflate"}
    with rasterio.open(path, "w", **profile) as ds:
        for number, value in enumerate(values, start=1):
            ds.write(np.full((SIDE_PX, SIDE_PX), value, dtype="uint16"), number)
            ds.set_band_description(number, BAND_NAMES[number - 1])
        ds.scales = (0.0001,) * len(values)
        ds.offsets = (0.0,) * len(values)


async def main() -> None:
    refuse_real_data()
    owner = LegacyUserContext().get_user_id()
    to_metres = Transformer.from_crs(4326, 3857, always_xy=True).transform
    to_degrees = Transformer.from_crs(3857, 4326, always_xy=True).transform
    cx, cy = to_metres(CENTRE_LON, CENTRE_LAT)
    half = PIXEL_M * SIDE_PX / 2
    west, north = cx - half, cy + half
    w, s = to_degrees(cx - half, cy - half)
    e, n = to_degrees(cx + half, cy + half)
    bounds = [w, s, e, n]
    ring = [list(to_degrees(cx + dx, cy + dy)) for dx, dy in (
        (-FIELD_HALF_M, -FIELD_HALF_M), (FIELD_HALF_M, -FIELD_HALF_M), (FIELD_HALF_M, FIELD_HALF_M),
        (-FIELD_HALF_M, FIELD_HALF_M), (-FIELD_HALF_M, -FIELD_HALF_M))]

    s3, bucket = await get_async_s3_client(), get_bucket_name()
    try:
        await store_files(s3, bucket, owner, west, north, ring)
    finally:
        await s3.__aexit__(None, None, None)  # the app keeps its client for good; a script closes its own
    await store_rows(owner, bounds)
    print(json.dumps(EXPECTED))


async def store_files(s3, bucket: str, owner: str, west: float, north: float, ring: list[list[float]]) -> None:
    with tempfile.TemporaryDirectory() as folder:
        for layer_id, _name, _day, values in FLIGHTS:
            path = os.path.join(folder, f"{layer_id}.tif")
            write_photo(path, west, north, values)
            with open(path, "rb") as f:
                await s3.put_object(Bucket=bucket, Key=key_of(layer_id), Body=f.read(), ContentType="image/tiff")

    # The April photo is the one prepared for taps, and holds the kept field. The line is one "moved by hand":
    # its area is measured by the same code that measures a reader's own line, never typed in here.
    april = FLIGHTS[0][0]
    await s3.put_object(Bucket=bucket, Key=field_taps.manifest_key(key_of(april)),
                        Body=json.dumps(field_taps.manifest("EPSG:3857", west, north, [(0, 0)])).encode(),
                        ContentType="application/json")
    first_tap = field_taps.Tap(lon=CENTRE_LON, lat=CENTRE_LAT, add=True)
    own = field_taps.moved_line(ring[:-1], first_tap)
    row = {"id": FIELD_ID, "taps": [{"lon": CENTRE_LON, "lat": CENTRE_LAT, "add": True}], "pick": 0, "corrections": 0,
           "kept_by": owner, "kept_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **own,
           "by_hand": True, "number": 1}
    row["centre"] = field_taps._centre(row["geometry"])  # noqa: SLF001  the stored record's own shape
    await field_taps._store_kept(s3, bucket, key_of(april), april, [row], 2)  # noqa: SLF001


async def store_rows(owner: str, bounds: list[float]) -> None:
    conn = await asyncpg.connect(_build_postgres_url())
    try:
        await conn.execute("SELECT set_config('app.user_id', '', false)")  # seeding: the admin branch of RLS
        layer_ids = [flight[0] for flight in FLIGHTS]
        await conn.execute("DELETE FROM map_layers WHERE layer_id = ANY($1::text[])", layer_ids)
        await conn.execute("DELETE FROM user_mundiai_maps WHERE id = $1", MAP)
        await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", PROJECT)
        await conn.execute(
            "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title, industry, maps) "
            "VALUES ($1, $2::uuid, '{}', '{}', 'Browser test farm', 'agriculture', ARRAY[$3])", PROJECT, owner, MAP)
        await conn.execute(
            "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
            "VALUES ($1, $2, $3::uuid, 'Browser test farm', $4::text[])", MAP, PROJECT, owner, layer_ids)
        for layer_id, name, day, values in FLIGHTS:
            await conn.execute(
                "INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, bounds, source_map_id) "
                "VALUES ($1, $2::uuid, $3, 'raster', $4::jsonb, $5, $6)", layer_id, owner, name,
                json.dumps({"cog_key": key_of(layer_id), "cog_status": "ready", "band_count": len(values),
                            "flown_on": day}), bounds, MAP)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
