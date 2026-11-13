"""Which plots a drone photo uses: the reader's own plot map when one is chosen, else the fields the reader
outlined on the photo by tapping (when there are any), else the plots Ingabe found.

One home for this choice, so the question cards and Sage number the same plots the same way.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any, Optional

from src.database.models import MapLayer
from src.database.rwanda_reader import RWANDA_INTERNAL_CONNECTION_NAME
from shapely.geometry import shape

from src.services import drone_plots, field_taps
from src.structures import async_read_conn

logger = logging.getLogger(__name__)

PLOT_MAP_KEY = "plot_map_layer_id"  # in the photo layer's metadata: the reader's chosen plot source
# Besides a map layer's id, the chosen source can be one of these two. With nothing chosen, the reader's outlined
# fields are the plots as soon as one is kept (Roger, 2026-11-13); FOUND is how a reader goes back to Ingabe's.
FIELDS = drone_plots.OUTLINED_FIELDS
FOUND = "found"
FIELDS_NAME = drone_plots.OUTLINED_FIELDS_NAME

# Polygon layers in the layer's project that overlap its outline: the maps a reader can choose as its plots.
# Layers Sage made as analysis (roof and tree outlines, hexagon grids) and layers read from the platform's own
# Rwanda tables (admin boundaries, sector indices) are not plot maps.
_PLOT_MAPS_SQL = """
SELECT l.layer_id, l.name, l.feature_count, l.last_edited
  FROM map_layers l
 WHERE l.type IN ('vector', 'postgis')
   AND l.geometry_type ILIKE '%polygon%'
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND l.metadata->>'analysis_kind' IS NULL
   AND NOT EXISTS (SELECT 1 FROM project_postgres_connections c
                    WHERE c.id = l.postgis_connection_id AND c.connection_name = $6)
   AND l.layer_id IN (
       SELECT unnest(m.layers) FROM user_mundiai_maps m
        WHERE m.soft_deleted_at IS NULL
          AND m.project_id IN (SELECT m2.project_id FROM user_mundiai_maps m2 WHERE $1 = ANY(m2.layers)))
 ORDER BY l.last_edited DESC NULLS LAST
"""


_fields_version: dict[str, str] = {}  # per photo: the version of the reader's outlined fields last measured


def photo_key(metadata: dict[str, Any]) -> str:
    """Copies of a photo layer point at the same stored image, so they share its plots."""
    return metadata["cog_key"]


async def _map_plots(user_id: str, map_layer_id: str) -> list[drone_plots.MapPlot]:
    async with async_read_conn("drone_plot_map", user_id=user_id) as conn:
        row = await conn.fetchrow("SELECT * FROM map_layers WHERE layer_id = $1", map_layer_id)
    if row is None:
        raise ValueError("the map layer is gone")
    async with await MapLayer(**dict(row)).get_ogr_source() as source:
        return await asyncio.to_thread(drone_plots.read_plot_map, source)


async def plot_maps(conn: Any, layer_id: str, bounds: list[float]) -> list[tuple[drone_plots.PlotMap, str]]:
    """(map, version) for each polygon layer the reader can choose; the version changes when the map is edited."""
    west, south, east, north = bounds
    rows = await conn.fetch(_PLOT_MAPS_SQL, layer_id, west, south, east, north, RWANDA_INTERNAL_CONNECTION_NAME)
    return [(drone_plots.PlotMap(layer_id=r["layer_id"], name=r["name"], shapes=r["feature_count"]),
             f"{r['layer_id']}:{r['last_edited'].isoformat() if r['last_edited'] else ''}") for r in rows]


async def outlined_fields(s3: Any, bucket: str, metadata: dict[str, Any]) -> Optional[tuple[drone_plots.PlotMap, str]]:
    """(source, version) for the fields the reader outlined on this photo, or None when there are none or the
    FIELD_TAPS switch is off. The version changes when a field is kept, removed or its line moved."""
    if not field_taps.enabled():
        return None
    rows = await field_taps.kept(s3, bucket, photo_key(metadata))
    if not rows:
        return None
    said = json.dumps([[row["id"], row["number"], row["geometry"]] for row in rows], sort_keys=True)
    return (drone_plots.PlotMap(layer_id=FIELDS, name=FIELDS_NAME, shapes=len(rows)),
            f"{FIELDS}:{hashlib.sha256(said.encode()).hexdigest()[:16]}")


async def plot_sources(conn: Any, s3: Any, bucket: str, layer_id: str, bounds: list[float],
                       metadata: dict[str, Any]) -> list[tuple[drone_plots.PlotMap, str]]:
    """Every source of plots the reader can choose for this photo besides Ingabe's own: their outlined fields
    first, then the polygon maps of the project that cover the photo."""
    fields = await outlined_fields(s3, bucket, metadata)
    return ([fields] if fields else []) + await plot_maps(conn, layer_id, bounds)


def chosen_source(metadata: dict[str, Any], maps: list[tuple[drone_plots.PlotMap, str]]) -> Optional[str]:
    """The id of the source in use: the reader's choice, else their outlined fields when they have any, else None
    (the plots Ingabe found)."""
    wanted = metadata.get(PLOT_MAP_KEY) or (FIELDS if any(m.layer_id == FIELDS for m, _ in maps) else None)
    return wanted if wanted != FOUND and any(m.layer_id == wanted for m, _ in maps) else None


async def _field_plots(s3: Any, bucket: str, metadata: dict[str, Any]) -> list[drone_plots.MapPlot]:
    """The reader's outlined fields as plots; each keeps its field number as its plot number."""
    rows = await field_taps.kept(s3, bucket, photo_key(metadata))
    return [drone_plots.MapPlot(shape(row["geometry"]), number=row["number"]) for row in rows]


async def plots_from_chosen_map(
    s3: Any, bucket: str, layer_id: str, metadata: dict[str, Any], user_id: str, cog_url: str,
    maps: list[tuple[drone_plots.PlotMap, str]],
) -> tuple[Optional[drone_plots.PlotSet], Optional[str], Optional[str]]:
    """(plots, chosen map layer id, error) for the plot map chosen for this photo; all None when none is chosen."""
    wanted = chosen_source(metadata, maps)
    chosen = next(((m, version) for m, version in maps if m.layer_id == wanted), None)
    if chosen is None:
        return None, None, None
    plot_map, version = chosen
    if plot_map.layer_id == FIELDS:
        # every keep or remove makes a new version; the one before it is measured plots nobody reads any more
        before = _fields_version.get(photo_key(metadata))
        if before and before != version:
            await drone_plots.forget_map_plots(s3, bucket, photo_key(metadata), before)
        _fields_version[photo_key(metadata)] = version
    try:
        plots = await drone_plots.load_map_plots(
            s3, bucket, photo_key(metadata), version, cog_url, plot_map.name,
            (lambda: _field_plots(s3, bucket, metadata)) if plot_map.layer_id == FIELDS
            else (lambda: _map_plots(user_id, plot_map.layer_id)))
    except Exception as exc:  # the cards say the map could not be read and use the plots Ingabe found
        logger.exception("plot map %s could not be read for %s", plot_map.layer_id, layer_id)
        return None, None, f"{plot_map.name} could not be read ({str(exc)[:120]})"
    return plots, plot_map.layer_id, None


async def current_plots(s3: Any, bucket: str, layer_id: str, bounds: Optional[list[float]], metadata: dict[str, Any],
                        user_id: str, cog_url: str) -> Optional[drone_plots.PlotSet]:
    """The photo's plots as the cards use them: from the chosen plot map, else the reader's outlined fields, else
    those Ingabe found (None if not yet)."""
    fields = await outlined_fields(s3, bucket, metadata)
    maps = [fields] if fields else []
    if metadata.get(PLOT_MAP_KEY) not in (None, FIELDS, FOUND) and bounds:
        async with async_read_conn("drone_plot_maps", user_id=user_id) as conn:
            maps += await plot_maps(conn, layer_id, list(bounds))
    if maps:
        plots, _, _ = await plots_from_chosen_map(s3, bucket, layer_id, metadata, user_id, cog_url, maps)
        if plots is not None:
            return plots
    return await drone_plots.load_plots(s3, bucket, photo_key(metadata))
