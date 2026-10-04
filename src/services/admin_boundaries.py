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

# Rwanda's 30 districts (official names).
RWANDA_DISTRICTS = [
    "Bugesera", "Gatsibo", "Kayonza", "Kirehe", "Ngoma", "Nyagatare",
    "Rwamagana", "Gasabo", "Kicukiro", "Nyarugenge", "Burera", "Gakenke",
    "Gicumbi", "Musanze", "Rulindo", "Gisagara", "Huye", "Kamonyi",
    "Muhanga", "Nyamagabe", "Nyanza", "Nyaruguru", "Ruhango",
    "Karongi", "Ngororero", "Nyabihu", "Nyamasheke", "Rubavu",
    "Rutsiro", "Rusizi",
]

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


# Map outlines per level: (table, id column, name column, parent columns).
_OUTLINE_SPEC = {
    "district": ("rwanda_district_boundaries", "district", "district", ()),
    "sector": ("rwanda_sector_boundaries", "sector_id", "sector_name", ("district_name",)),
    "cell": ("rwanda_cell_boundaries", "cell_id", "cell_name", ("sector_name", "district_name")),
    "village": ("rwanda_village_boundaries", "village_id", "village_name",
                ("cell_name", "sector_name", "district_name")),
}
# Simplification tolerance (degrees, ~1 m per 0.00001): coarse enough to keep
# responses small, fine enough that a unit's outline stays recognisable.
_OUTLINE_TOLERANCE = {"district": 0.002, "sector": 0.0008, "cell": 0.0003, "village": 0.0001}
OUTLINE_LIMIT = 4000


async def admin_outlines(conn, level: str, bbox: Optional[tuple[float, float, float, float]]) -> dict:
    """Outlines of every ``level`` unit intersecting ``bbox`` (west, south, east, north).

    Sectors, cells and villages need a bbox; at most OUTLINE_LIMIT units are
    returned and ``truncated`` says when more were cut.
    """
    if level not in _OUTLINE_SPEC:
        raise ValueError(f"level must be one of {', '.join(_OUTLINE_SPEC)}")
    if bbox is None and level != "district":
        raise ValueError(f"a bbox is needed for {level} outlines")
    table, id_col, name_col, parents = _OUTLINE_SPEC[level]
    parent_sql = "".join(f", {p}" for p in parents)
    where = "WHERE geom && ST_MakeEnvelope($1, $2, $3, $4, 4326)" if bbox else ""
    rows = await conn.fetch(
        f"SELECT {id_col}::text AS id, {name_col} AS name{parent_sql}, "
        f"ST_AsGeoJSON(ST_SimplifyPreserveTopology(geom, {_OUTLINE_TOLERANCE[level]}), 5) AS geometry "
        f"FROM {table} {where} ORDER BY {id_col} LIMIT {OUTLINE_LIMIT + 1}",
        *(bbox or ()),
    )
    features = [
        {
            "type": "Feature",
            "geometry": json.loads(r["geometry"]),
            "properties": {"id": r["id"], "name": r["name"], "level": level,
                           **{p.removesuffix("_name"): r[p] for p in parents}},
        }
        for r in rows[:OUTLINE_LIMIT] if r["geometry"]
    ]
    return {"type": "FeatureCollection", "level": level,
            "truncated": len(rows) > OUTLINE_LIMIT, "features": features}


async def units_per_district(conn) -> dict[str, dict[str, int]]:
    """How many sectors, cells and villages each district has, keyed by lower-case name."""
    counts: dict[str, dict[str, int]] = {}
    for level in ("sector", "cell", "village"):
        table = _OUTLINE_SPEC[level][0]
        for r in await conn.fetch(f"SELECT lower(district_name) AS d, count(*) AS n FROM {table} GROUP BY 1"):
            counts.setdefault(r["d"], {})[level] = r["n"]
    return counts
