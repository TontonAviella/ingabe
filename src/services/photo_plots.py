"""Which plots a drone photo uses: the reader's own plot map when one is chosen, else the plots Ingabe found.

One home for this choice, so the question cards and Sage number the same plots the same way.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from src.database.models import MapLayer
from src.services import drone_plots
from src.structures import async_read_conn

logger = logging.getLogger(__name__)

PLOT_MAP_KEY = "plot_map_layer_id"  # in the photo layer's metadata: the reader's chosen plot map

# Polygon layers in the layer's project that overlap its outline: the maps a reader can choose as its plots.
# Outlines Sage drew on photos (roofs, trees) are not plot maps.
_PLOT_MAPS_SQL = """
SELECT l.layer_id, l.name, l.feature_count, l.last_edited
  FROM map_layers l
 WHERE l.type IN ('vector', 'postgis')
   AND l.geometry_type ILIKE '%polygon%'
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND COALESCE(l.metadata->>'source', '') <> 'sage_raster_object_candidates'
   AND l.layer_id IN (
       SELECT unnest(m.layers) FROM user_mundiai_maps m
        WHERE m.soft_deleted_at IS NULL
          AND m.project_id IN (SELECT m2.project_id FROM user_mundiai_maps m2 WHERE $1 = ANY(m2.layers)))
 ORDER BY l.last_edited DESC NULLS LAST
"""


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
    rows = await conn.fetch(_PLOT_MAPS_SQL, layer_id, west, south, east, north)
    return [(drone_plots.PlotMap(layer_id=r["layer_id"], name=r["name"], shapes=r["feature_count"]),
             f"{r['layer_id']}:{r['last_edited'].isoformat() if r['last_edited'] else ''}") for r in rows]


async def plots_from_chosen_map(
    s3: Any, bucket: str, layer_id: str, metadata: dict[str, Any], user_id: str, cog_url: str,
    maps: list[tuple[drone_plots.PlotMap, str]],
) -> tuple[Optional[drone_plots.PlotSet], Optional[str], Optional[str]]:
    """(plots, chosen map layer id, error) for the plot map chosen for this photo; all None when none is chosen."""
    chosen = next(((m, version) for m, version in maps if m.layer_id == metadata.get(PLOT_MAP_KEY)), None)
    if chosen is None:
        return None, None, None
    plot_map, version = chosen
    try:
        plots = await drone_plots.load_map_plots(
            s3, bucket, photo_key(metadata), version, cog_url, plot_map.name,
            lambda: _map_plots(user_id, plot_map.layer_id))
    except Exception as exc:  # the cards say the map could not be read and use the plots Ingabe found
        logger.exception("plot map %s could not be read for %s", plot_map.layer_id, layer_id)
        return None, None, f"{plot_map.name} could not be read ({str(exc)[:120]})"
    return plots, plot_map.layer_id, None


async def current_plots(s3: Any, bucket: str, layer_id: str, bounds: Optional[list[float]], metadata: dict[str, Any],
                        user_id: str, cog_url: str) -> Optional[drone_plots.PlotSet]:
    """The photo's plots as the cards use them: from the chosen plot map, else those Ingabe found (None if not yet)."""
    if metadata.get(PLOT_MAP_KEY) and bounds:
        async with async_read_conn("drone_plot_maps", user_id=user_id) as conn:
            maps = await plot_maps(conn, layer_id, list(bounds))
        plots, _, _ = await plots_from_chosen_map(s3, bucket, layer_id, metadata, user_id, cog_url, maps)
        if plots is not None:
            return plots
    return await drone_plots.load_plots(s3, bucket, photo_key(metadata))
