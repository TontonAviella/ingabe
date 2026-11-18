#!/usr/bin/env python3
"""A real photo in the browser-test copy of the app: this laptop's own layers, shown in a test project.

Local only (never CI: there is no real photo there). Run inside the test copy:

    docker exec mundi-app-e2e python scripts/e2e_real_photo.py --project PE2EREALAGRI --industry agriculture LAYER_ID ...

The layer rows are READ from the live local database (in a read-only transaction; nothing is written there) and
written to the test database under new layer ids, in a project owned by the shared no-sign-in user. The stored
files are not copied: the new rows point at the same files, which the test copy reads. What belongs to a layer
(kept fields, turned-down taps) is stored under the layer's id, so fields kept in the test never appear on the
real layer; what belongs to the stored photo (found plots, prepared windows) is shared and only read.

Like the seed, it refuses to write to the live database or to run where people sign in.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

import asyncpg

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.e2e_seed import LIVE_DATABASE, refuse_real_data  # noqa: E402
from src.database.pool import _build_postgres_url  # noqa: E402
from src.dependencies.session import LegacyUserContext  # noqa: E402
from src.services import industry as industries  # noqa: E402

COLUMNS = ("name", "s3_key", "type", "raster_cog_url", "metadata", "bounds", "geometry_type", "feature_count",
           "size_bytes", "remote_url")


def test_id(layer_id: str, project: str) -> str:
    """The copy's layer id: the same for the same layer in the same test project, and never the real one."""
    return "LE" + hashlib.sha256(f"{project}|{layer_id}".encode()).hexdigest()[:10]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="id of the test project, e.g. PE2EREALAGRI")
    parser.add_argument("--industry", default=industries.DEFAULT_INDUSTRY, choices=sorted(industries.INDUSTRIES))
    parser.add_argument("--title", default=None)
    parser.add_argument("layers", nargs="+", help="layer ids in the live local database, in the order to show")
    args = parser.parse_args()
    refuse_real_data()
    owner = LegacyUserContext().get_user_id()
    test_url = _build_postgres_url()
    live_url = test_url.rsplit("/", 1)[0] + "/" + LIVE_DATABASE

    live = await asyncpg.connect(live_url)
    try:
        async with live.transaction(readonly=True):
            await live.execute("SELECT set_config('app.user_id', '', true)")  # the admin branch of RLS, to read
            rows = [await live.fetchrow(f"SELECT layer_id, {', '.join(COLUMNS)} FROM map_layers WHERE layer_id = $1", lid)
                    for lid in args.layers]
    finally:
        await live.close()
    missing = [lid for lid, row in zip(args.layers, rows) if row is None]
    if missing:
        sys.exit(f"not in the live local database: {', '.join(missing)}")

    map_id = "M" + args.project[1:]
    new_ids = [test_id(row["layer_id"], args.project) for row in rows]
    conn = await asyncpg.connect(test_url)
    try:
        await conn.execute("SELECT set_config('app.user_id', '', false)")  # seeding: the admin branch of RLS
        title = args.title or f"Real photo, {industries.INDUSTRIES[args.industry]['label']}"
        # All or nothing, and the project and its map are kept when they exist: a map someone asked Sage about
        # cannot be deleted (its chat points at it), and a half-done run left the project without its photo.
        async with conn.transaction():
            await conn.execute("DELETE FROM map_layers WHERE source_map_id = $1", map_id)
            await conn.execute(
                "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title, industry, maps) "
                "VALUES ($1, $2::uuid, '{}', '{}', $3, $4, ARRAY[$5]) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, industry = EXCLUDED.industry, maps = EXCLUDED.maps",
                args.project, owner, title, args.industry, map_id)
            await conn.execute(
                "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
                "VALUES ($1, $2, $3::uuid, $4, $5::text[]) "
                "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, layers = EXCLUDED.layers",
                map_id, args.project, owner, title, new_ids)
            for new_id, row in zip(new_ids, rows):
                values = [row[c] for c in COLUMNS]
                await conn.execute(
                    f"INSERT INTO map_layers (layer_id, owner_uuid, source_map_id, {', '.join(COLUMNS)}) "
                    f"VALUES ($1, $2::uuid, $3, {', '.join(f'${i}' for i in range(4, 4 + len(COLUMNS)))})",
                    new_id, owner, map_id, *values)
    finally:
        await conn.close()
    print(json.dumps({"project": args.project, "map": map_id, "industry": args.industry,
                      "layers": dict(zip(args.layers, new_ids))}))


if __name__ == "__main__":
    asyncio.run(main())
