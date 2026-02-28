"""Shared Rwanda admin boundary geometry lookup with LRU caching.

Canonical source for looking up GeoJSON geometries from PostGIS admin
boundary tables.  Used by worldcover_router, rwanda_routes, and any
other module that needs admin boundary geometries.
"""

import json
import logging
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# Admin level → (table_name, column_name)
_ADMIN_LEVELS = {
    "village": ("rwanda_village_boundaries", "village_name"),
    "cell": ("rwanda_cell_boundaries", "cell_name"),
    "sector": ("rwanda_sector_boundaries", "sector_name"),
    "district": ("rwanda_district_boundaries", "district"),
}

# Priority order: most specific first
_ADMIN_PRIORITY = ("village", "cell", "sector", "district")

# Bounded LRU cache for geometry lookups
_CACHE_MAX = 2000
_cache: OrderedDict[str, dict] = OrderedDict()


def _resolve_admin_level(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
) -> Optional[tuple[str, str]]:
    """Resolve which admin level to query.

    Returns (level, name) tuple or None if no filter specified.
    Priority: village > cell > sector > district.
    """
    values = {"village": village, "cell": cell, "sector": sector, "district": district}
    for level in _ADMIN_PRIORITY:
        if values[level]:
            return level, values[level]
    return None


async def lookup_admin_geometry(
    district: Optional[str] = None,
    sector: Optional[str] = None,
    cell: Optional[str] = None,
    village: Optional[str] = None,
) -> Optional[dict]:
    """Fetch GeoJSON geometry for a Rwanda admin boundary from PostGIS.

    Returns cached result if available.  Priority: village > cell > sector > district.
    Uses read-only connection pool.  Includes sector fallback via cell union.
    """
    resolved = _resolve_admin_level(district, sector, cell, village)
    if resolved is None:
        return None

    level, name = resolved
    cache_key = f"{level}:{name.lower()}"

    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        return _cache[cache_key]

    try:
        from src.structures import get_async_read_connection

        async with get_async_read_connection() as conn:
            table, column = _ADMIN_LEVELS[level]
            row = await conn.fetchrow(
                f"SELECT ST_AsGeoJSON(geom)::text FROM {table} "
                f"WHERE LOWER({column}) = LOWER($1) LIMIT 1",
                name,
            )

            # Sector fallback: union cells if sector table lookup fails
            if not (row and row[0]) and level == "sector":
                row = await conn.fetchrow(
                    "SELECT ST_AsGeoJSON(ST_Union(geom))::text FROM rwanda_cell_boundaries "
                    "WHERE LOWER(sector_name) = LOWER($1)",
                    name,
                )

            if row and row[0]:
                geom = json.loads(row[0])
                _cache[cache_key] = geom
                if len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
                return geom
    except Exception as e:
        logger.warning("Admin geometry lookup failed for %s: %s", cache_key, e)

    return None
