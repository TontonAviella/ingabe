"""Persistence and rendering helpers for remote Cloud-Optimized GeoTIFF layers."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlparse

from src.database.pool import async_conn
from src.utils import generate_id


@dataclass(frozen=True)
class PersistedRemoteCogLayer:
    layer_id: str
    style_id: str
    source_id: str
    tile_url: str


def infer_remote_cog_metadata(remote_url: str) -> dict[str, Any]:
    """Infer stable provenance embedded in known public COG URLs."""
    parsed = urlparse(remote_url)
    if parsed.hostname != "sentinel-cogs.s3.us-west-2.amazonaws.com":
        return {}

    match = re.search(
        r"/(S2([ABCD])_[0-9A-Z]+_(\d{8})_\d+_L2A)/",
        parsed.path,
    )
    if not match:
        return {
            "source_catalog": "earth_search",
            "collection": "sentinel-2-l2a",
        }

    scene_id, platform_suffix, compact_date = match.groups()
    scene_date = datetime.strptime(compact_date, "%Y%m%d").date().isoformat()
    return {
        "source_catalog": "earth_search",
        "collection": "sentinel-2-l2a",
        "scene_id": scene_id,
        "scene_date": scene_date,
        "platform": f"sentinel-2{platform_suffix.lower()}",
    }


def build_remote_cog_tile_url(metadata: dict[str, Any]) -> str:
    remote_url = str(metadata.get("remote_cog_url") or "")
    if not remote_url:
        raise ValueError("remote_cog_url is required")

    params = [
        f"url={quote(remote_url, safe='')}",
        f"expression={quote(str(metadata.get('expression') or 'visual'), safe='')}",
    ]
    for key in ("colormap", "rescale", "band_index"):
        value = metadata.get(key)
        if value not in (None, ""):
            params.append(f"{key}={quote(str(value), safe='')}")
    return "/api/cog-tiles/{z}/{x}/{y}.png?" + "&".join(params)


def build_remote_cog_maplibre_layer(
    layer_id: str,
    metadata: dict[str, Any],
    bounds: list[float] | None,
) -> tuple[str, dict[str, Any], dict[str, Any]]:
    source_id = f"remote-cog-source-{layer_id}"
    source: dict[str, Any] = {
        "type": "raster",
        "tiles": [build_remote_cog_tile_url(metadata)],
        "tileSize": 256,
        "minzoom": int(metadata.get("minzoom", 0)),
        "maxzoom": int(metadata.get("maxzoom", 14)),
    }
    if bounds and len(bounds) == 4:
        source["bounds"] = [float(value) for value in bounds]

    layer = {
        "id": f"raster-layer-{layer_id}",
        "type": "raster",
        "source": source_id,
        "paint": {"raster-opacity": float(metadata.get("opacity", 0.9))},
    }
    return source_id, source, layer


async def persist_remote_cog_layer(
    *,
    map_id: str,
    user_uuid: str,
    layer_name: str,
    remote_url: str,
    bounds: list[float],
    expression: str,
    style_hint: str,
    colormap: str = "",
    rescale: str = "",
    band_index: int | None = None,
    extra_metadata: dict[str, Any] | None = None,
    partner_id: str | None = None,
) -> PersistedRemoteCogLayer:
    # Import lazily because map_service imports the pure render helper above.
    from src.services.map_service import validate_remote_url

    validated_url = validate_remote_url(remote_url, "raster")
    layer_id = generate_id(prefix="L")
    style_id = generate_id(prefix="S")
    metadata: dict[str, Any] = {
        "remote_cog": True,
        "remote_cog_url": validated_url,
        "expression": expression,
        "style_hint": style_hint,
        "colormap": colormap,
        "rescale": rescale,
        "band_index": band_index,
        "maxzoom": 14,
        "opacity": 0.9,
    }
    metadata.update(infer_remote_cog_metadata(validated_url))
    if extra_metadata:
        metadata.update(
            {key: value for key, value in extra_metadata.items() if value not in (None, "")}
        )

    async with async_conn(
        "persist_remote_cog_layer",
        user_id=user_uuid,
        partner_id=partner_id,
    ) as conn:
        async with conn.transaction():
            map_row = await conn.fetchrow(
                """
                SELECT m.layers
                FROM user_mundiai_maps m
                JOIN user_mundiai_projects p ON p.id = m.project_id
                WHERE m.id = $1
                  AND m.soft_deleted_at IS NULL
                  AND p.soft_deleted_at IS NULL
                  AND (
                    p.owner_uuid = $2::uuid
                    OR $2::uuid = ANY(COALESCE(p.editor_uuids, ARRAY[]::uuid[]))
                  )
                FOR UPDATE OF m
                """,
                map_id,
                user_uuid,
            )
            if not map_row:
                raise PermissionError(f"Map {map_id} is not editable by this user")

            await conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, metadata, bounds, source_map_id,
                 remote_url, created_on, last_edited)
                VALUES ($1, $2::uuid, $3, 'raster', $4::jsonb, $5, $6, $7,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                layer_id,
                user_uuid,
                layer_name,
                json.dumps(metadata),
                bounds,
                map_id,
                validated_url,
            )
            await conn.execute(
                """
                INSERT INTO layer_styles
                (style_id, layer_id, style_json, created_by, created_on)
                VALUES ($1, $2, '[]'::jsonb, $3::uuid, CURRENT_TIMESTAMP)
                """,
                style_id,
                layer_id,
                user_uuid,
            )
            await conn.execute(
                """
                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                VALUES ($1, $2, $3)
                """,
                map_id,
                layer_id,
                style_id,
            )
            current_layers = list(map_row["layers"] or [])
            await conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = $1, last_edited = CURRENT_TIMESTAMP
                WHERE id = $2
                """,
                current_layers + [layer_id],
                map_id,
            )

    metadata_for_render = metadata
    source_id, source, _ = build_remote_cog_maplibre_layer(
        layer_id, metadata_for_render, bounds
    )
    return PersistedRemoteCogLayer(
        layer_id=layer_id,
        style_id=style_id,
        source_id=source_id,
        tile_url=str(source["tiles"][0]),
    )
