"""A tower photo inspection seeded for the tests of the tower routes and Sage's tower tools: a project of an industry,
a map showing a vector layer recorded as a photo inspection, the committed tower photo stored as its original
(test_fixtures/example_finder/tower_dishes.jpg, 1333 x 750 px) and every photo's facts kept beside it, as the photo
upload leaves them (src/upload/handlers/photo_set_handler.py)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import asyncpg

from src.database.pool import _build_postgres_url
from src.services import example_finder, photo_inspections, tower_inspection
from src.services.photo_inspections import PhotoFacts
from src.utils import get_async_s3_client, get_bucket_name, get_s3_client

PHOTO = (Path(__file__).parent / "example_finder" / "tower_dishes.jpg").read_bytes()
SHA = hashlib.sha256(PHOTO).hexdigest()
MARKS = json.loads((Path(__file__).parent / "example_finder" / "tower_dishes.json").read_text())["marks"]


def variant(n: int) -> bytes:
    """Another photo file showing the same picture: the tower photo with n bytes after its end (a JPEG decoder
    ignores them), so it has a sha256 of its own."""
    return PHOTO + bytes([n % 256]) * n


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tower_facts(sha: str = SHA, name: str = "DJI_0395.JPG", **changes: Any) -> PhotoFacts:
    """The committed tower photo's facts as the upload keeps them: taken at an angle (DJI gimbal pitch -30)."""
    base = PhotoFacts(sha256=sha, filename=name, width=1333, height=750, taken_at="2026-10-09T14:03:22",
                      lat=-1.9441, lon=30.0619, altitude_m=1530.0, relative_altitude_m=45.2, gimbal_pitch=-30.0,
                      gimbal_yaw=-87.3, flight_yaw=-88.1, make="DJI", model="M3E", orientation=1)
    return replace(base, **changes)


async def admin() -> asyncpg.Connection:
    conn = await asyncpg.connect(_build_postgres_url())
    await conn.execute("SELECT set_config('app.user_id', '', false)")  # seeding: the admin branch of row security
    return conn


class Inspections:
    """Seeds tower inspections and removes everything seeded (rows and stored files)."""

    def __init__(self) -> None:
        self.made: list[dict[str, Any]] = []

    async def make(self, owner: Optional[str] = None, industry: str = "telecom", editors: tuple[str, ...] = (),
                   viewers: tuple[str, ...] = (), facts: Optional[list[PhotoFacts]] = None, keep_facts: bool = True,
                   is_inspection: bool = True, project: Optional[str] = None,
                   files: Optional[dict[str, bytes]] = None) -> dict[str, Any]:
        """A layer in a project of `industry` (a new project unless `project` is given, then another layer in it).
        Each photo's stored file is `files[sha256]`, else the tower photo."""
        owner = owner or str(uuid.uuid4())
        n = uuid.uuid4().hex[:10]  # ids are 12 characters, as the app makes them
        ids = {"owner": owner, "project": project or f"PT{n}", "map": f"MT{n}", "layer": f"LT{n}",
               "new_project": project is None}
        prefix = photo_inspections.storage_prefix(ids["project"], ids["layer"])
        facts = facts if facts is not None else [tower_facts()]
        for f in facts:
            get_s3_client().put_object(Bucket=get_bucket_name(), Key=f"{prefix}{f.sha256}.jpg",
                                       Body=(files or {}).get(f.sha256, PHOTO))
        if keep_facts:
            await photo_inspections.save_facts(await get_async_s3_client(), get_bucket_name(), prefix, facts)
        metadata = {"photo_inspection": {**photo_inspections.summary(facts, prefix)}} if is_inspection else {}
        conn = await admin()
        try:
            if project is None:  # a new project and its map, listed in projects.maps as the app does
                await conn.execute(
                    "INSERT INTO user_mundiai_projects (id, owner_uuid, editor_uuids, viewer_uuids, title, industry, "
                    "maps) VALUES ($1, $2::uuid, $3::uuid[], $4::uuid[], 'tower test', $5, ARRAY[$6])",
                    ids["project"], owner, list(editors), list(viewers), industry, ids["map"])
                await conn.execute(
                    "INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) "
                    "VALUES ($1, $2, $3::uuid, 'tower test', ARRAY[$4])",
                    ids["map"], ids["project"], owner, ids["layer"])
            else:  # another layer on the project's current map
                ids["map"] = await conn.fetchval(
                    "UPDATE user_mundiai_maps SET layers = array_append(layers, $1) WHERE id = "
                    "(SELECT maps[array_length(maps, 1)] FROM user_mundiai_projects WHERE id = $2) RETURNING id",
                    ids["layer"], ids["project"])
            await conn.execute(
                "INSERT INTO map_layers (layer_id, owner_uuid, name, type, metadata, source_map_id) "
                "VALUES ($1, $2::uuid, $3, 'vector', $4::jsonb, $5)",
                ids["layer"], owner, f"Rebero inspection {n}", json.dumps(metadata), ids["map"])
        finally:
            await conn.close()
        self.made.append(ids)
        return ids

    async def remove(self) -> None:
        conn = await admin()
        store, bucket = get_s3_client(), get_bucket_name()
        try:
            for ids in reversed(self.made):
                await conn.execute("DELETE FROM map_layers WHERE layer_id = $1", ids["layer"])
                # A test may fork maps from this one (parent_map_id is a foreign key): unlink them first.
                await conn.execute("UPDATE user_mundiai_maps SET parent_map_id = NULL WHERE parent_map_id = $1",
                                   ids["map"])
                await conn.execute("DELETE FROM user_mundiai_maps WHERE id = $1", ids["map"])
                if ids["new_project"]:
                    await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", ids["project"])
                for root in (f"inspections/{ids['project']}/", f"{tower_inspection.STORE_PREFIX}/{ids['project']}/",
                             f"{example_finder.STORE_PREFIX}/{ids['project']}/"):
                    for item in store.list_objects_v2(Bucket=bucket, Prefix=root).get("Contents", []):
                        store.delete_object(Bucket=bucket, Key=item["Key"])
        finally:
            await conn.close()
