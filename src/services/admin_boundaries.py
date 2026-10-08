"""Shared Rwanda admin boundary geometry lookup with LRU caching.

Canonical source for looking up GeoJSON geometries from PostGIS admin
boundary tables.  Used by worldcover_router, rwanda_routes, and any
other module that needs admin boundary geometries.
"""

import difflib
import json
import logging

import asyncpg
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


# Listing units: level -> (table, name column, parent columns from nearest to farthest).
_LIST_SPEC = {
    "district": ("rwanda_district_boundaries", "district", ()),
    "sector": ("rwanda_sector_boundaries", "sector_name", ("district_name",)),
    "cell": ("rwanda_cell_boundaries", "cell_name", ("sector_name", "district_name")),
    "village": ("rwanda_village_boundaries", "village_name",
                ("cell_name", "sector_name", "district_name")),
}
LIST_LIMIT = 1000


async def list_admin_units(
    conn, level: str, *, district: str = "", sector: str = "", cell: str = ""
) -> dict:
    """The ``level`` units inside the named parents, with an exact count.

    Parent names match case-insensitively. Each row is one boundary polygon, so
    a name the source data repeats is listed (and counted) twice. Raises
    ValueError, with a message the model can act on, when no parent is given,
    a parent name is unknown (with close matches) or a sector/cell name exists
    in more than one place.
    """
    if level not in _LIST_SPEC:
        raise ValueError(f"level must be one of {', '.join(_LIST_SPEC)}")
    table, name_col, parent_cols = _LIST_SPEC[level]
    given = {
        col: value.strip()
        for col, value in (("district_name", district), ("sector_name", sector), ("cell_name", cell))
        if col in parent_cols and value and value.strip()
    }
    if parent_cols and not given:
        raise ValueError(f"name the district (or sector or cell) whose {level}s to list")

    where = " AND ".join(f"lower({col}) = lower(${i})" for i, col in enumerate(given, 1))
    params = list(given.values())
    within: dict[str, str] = {}
    if given:
        # The most specific named parent must be one place, not a name shared by several.
        nearest = next(col for col in parent_cols if col in given)
        place_cols = parent_cols[parent_cols.index(nearest):]
        places = await conn.fetch(
            f"SELECT DISTINCT {', '.join(place_cols)} FROM {table} WHERE {where}", *params
        )
        if not places:
            raise ValueError(await _unknown_parent_message(conn, level, given))
        if len(places) > 1:
            options = "; ".join(", ".join(r[c] for c in place_cols) for r in places)
            label = nearest.removesuffix("_name")
            raise ValueError(
                f"{label} {given[nearest]!r} exists in more than one place ({options}); "
                f"name its district too"
            )
        within = {c.removesuffix("_name"): places[0][c] for c in place_cols}
        detail_cols = parent_cols[:parent_cols.index(nearest)]
    else:
        detail_cols = ()

    where_sql = f"WHERE {where}" if where else ""
    count = await conn.fetchval(f"SELECT count(*) FROM {table} {where_sql}", *params)
    columns = ", ".join((name_col, *detail_cols))
    rows = await conn.fetch(
        f"SELECT {columns} FROM {table} {where_sql} "
        f"ORDER BY {', '.join((*reversed(detail_cols), name_col))} LIMIT {LIST_LIMIT}",
        *params,
    )
    units = [
        {"name": r[name_col], **{c.removesuffix("_name"): r[c] for c in detail_cols}}
        for r in rows
    ]
    return {"level": level, "within": within, "count": count, "units": units,
            "truncated": count > len(units)}


async def _unknown_parent_message(conn, level: str, given: dict[str, str]) -> str:
    """Say which named parent does not exist (with the closest real names), or
    where the most specific one really is when the names do not fit together."""
    for col, value in given.items():
        label = col.removesuffix("_name")
        table, name_col, _ = _LIST_SPEC[label]
        exists = await conn.fetchval(
            f"SELECT 1 FROM {table} WHERE lower({name_col}) = lower($1) LIMIT 1", value
        )
        if not exists:
            by_lower = {r[0].lower(): r[0] for r in await conn.fetch(f"SELECT DISTINCT {name_col} FROM {table}") if r[0]}
            close = [by_lower[m] for m in difflib.get_close_matches(value.lower(), list(by_lower), n=3, cutoff=0.6)]
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            return f"no {label} named {value!r}{hint}"
    nearest = next((c for c in ("cell_name", "sector_name") if c in given), None)
    if nearest is None:
        return f"no {level}s are recorded for district {given['district_name']!r}"
    label = nearest.removesuffix("_name")
    table, name_col, parents = _LIST_SPEC[label]
    rows = await conn.fetch(
        f"SELECT DISTINCT {', '.join(parents)} FROM {table} WHERE lower({name_col}) = lower($1)",
        given[nearest],
    )
    places = "; ".join(", ".join(r[c] for c in parents) for r in rows)
    return f"{label} {given[nearest]!r} is not in the place named; it is in: {places}"


async def units_per_district(conn) -> dict[str, dict[str, int]]:
    """How many sectors, cells and villages each district has, keyed by lower-case name."""
    counts: dict[str, dict[str, int]] = {}
    for level in ("sector", "cell", "village"):
        table = _OUTLINE_SPEC[level][0]
        for r in await conn.fetch(f"SELECT lower(district_name) AS d, count(*) AS n FROM {table} GROUP BY 1"):
            counts.setdefault(r["d"], {})[level] = r["n"]
    return counts


def _admin_sql_literal(value: object) -> str:
    return "'" + str(value).replace("'", "''") + "'"


async def resolve_admin_boundary(
    conn,
    args: dict[str, object],
) -> dict[str, object]:
    """Resolve a request to show one Rwanda admin unit (or every unit of a level inside parents).

    `args`: admin_level (province|district|sector|cell|village|auto), name ("*" or "all" for every
    unit), and optional district / sector / cell parents. Returns a status (success, ambiguous,
    not_found, error) with the PostGIS query that draws the result and its bounds.
    """
    requested_level = str(args.get("admin_level") or "auto").strip().lower()
    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "error": "Missing boundary name."}

    levels = {
        "province": {
            "table": "rwanda_province_boundaries",
            "name_col": "province",
            "attrs": ["province"],
            "label": "province",
        },
        "district": {
            "table": "rwanda_district_boundaries",
            "name_col": "district",
            "attrs": ["district"],
            "label": "district",
        },
        "sector": {
            "table": "rwanda_sector_boundaries",
            "name_col": "sector_name",
            "attrs": ["sector_name", "district_name"],
            "label": "sector",
        },
        "cell": {
            "table": "rwanda_cell_boundaries",
            "name_col": "cell_name",
            "attrs": ["cell_name", "sector_name", "district_name"],
            "label": "cell",
        },
        "village": {
            "table": "rwanda_village_boundaries",
            "name_col": "village_name",
            "attrs": ["village_name", "cell_name", "sector_name", "district_name"],
            "label": "village",
        },
    }

    def _filters_for(level: str, *, sql: bool) -> tuple[list[str], list[object]]:
        spec = levels[level]
        filters: list[str] = []
        params: list[object] = []
        if name not in {"*", "all"}:
            if sql:
                filters.append(f"LOWER({spec['name_col']}) = LOWER({_admin_sql_literal(name)})")
            else:
                params.append(name)
                filters.append(f"LOWER({spec['name_col']}) = LOWER(${len(params)})")
        for arg_key, col in (
            ("district", "district_name"),
            ("sector", "sector_name"),
            ("cell", "cell_name"),
        ):
            value = args.get(arg_key)
            if not value:
                continue
            available_cols = set(spec["attrs"]) | {spec["name_col"]}
            if col not in available_cols:
                continue
            if sql:
                filters.append(f"LOWER({col}) = LOWER({_admin_sql_literal(value)})")
            else:
                params.append(value)
                filters.append(f"LOWER({col}) = LOWER(${len(params)})")
        return filters, params

    async def _match(level: str) -> dict[str, object]:
        spec = levels[level]
        filters, params = _filters_for(level, sql=False)
        where = " AND ".join(filters) if filters else "TRUE"
        try:
            rows = await conn.fetch(
                f"""
                SELECT {', '.join(spec['attrs'])},
                       ST_XMin(ST_Extent(geom)) AS xmin,
                       ST_YMin(ST_Extent(geom)) AS ymin,
                       ST_XMax(ST_Extent(geom)) AS xmax,
                       ST_YMax(ST_Extent(geom)) AS ymax,
                       COUNT(*) OVER() AS match_count
                FROM {spec['table']}
                WHERE {where}
                GROUP BY {', '.join(spec['attrs'])}
                ORDER BY {', '.join(spec['attrs'])}
                LIMIT 12
                """,
                *params,
            )
        except asyncpg.exceptions.UndefinedTableError:
            logger.warning("Admin boundary table missing: %s", spec["table"])
            return {"status": "not_found", "admin_level": level}
        if not rows:
            return {"status": "not_found", "admin_level": level}
        total = int(rows[0]["match_count"])
        candidates = [dict(row) for row in rows]

        sql_filters, _ = _filters_for(level, sql=True)
        sql_where = " AND ".join(sql_filters) if sql_filters else "TRUE"
        attr_select = ", ".join(spec["attrs"])
        query = (
            f"SELECT ROW_NUMBER() OVER()::bigint AS id, {attr_select}, geom "
            f"FROM {spec['table']} WHERE {sql_where}"
        )
        if name in {"*", "all"} or total > 1:
            extent_row = await conn.fetchrow(
                f"""
                SELECT ST_XMin(ST_Extent(geom)) AS xmin,
                       ST_YMin(ST_Extent(geom)) AS ymin,
                       ST_XMax(ST_Extent(geom)) AS xmax,
                       ST_YMax(ST_Extent(geom)) AS ymax
                FROM {spec['table']}
                WHERE {where}
                """,
                *params,
            )
            bounds_source = extent_row or rows[0]
        else:
            bounds_source = rows[0]
        bounds = [
            float(bounds_source["xmin"]),
            float(bounds_source["ymin"]),
            float(bounds_source["xmax"]),
            float(bounds_source["ymax"]),
        ]
        first = dict(rows[0])
        display_name = (
            str(first.get(spec["name_col"]) or name)
            if name not in {"*", "all"}
            else f"{level.title()} Boundaries"
        )
        if total > 1 and name not in {"*", "all"}:
            return {
                "status": "ambiguous",
                "admin_level": level,
                "admin_name": display_name,
                "query": query,
                "bounds": bounds,
                "feature_count": total,
                "attribute_columns": spec["attrs"],
                "layer_name": f"{display_name} {level.title()} Matches",
                "candidates": candidates,
                "match_count": total,
            }
        return {
            "status": "success",
            "admin_level": level,
            "admin_name": display_name,
            "query": query,
            "bounds": bounds,
            "feature_count": total if name in {"*", "all"} else 1,
            "attribute_columns": spec["attrs"],
            "layer_name": (
                f"{display_name} {level.title()} Boundary"
                if name not in {"*", "all"}
                else f"{display_name}"
            ),
        }

    search_levels = (
        list(levels)
        if requested_level == "auto"
        else [requested_level]
    )
    for level in search_levels:
        if level not in levels:
            continue
        result = await _match(level)
        if result["status"] != "not_found":
            return result
    return {
        "status": "not_found",
        "admin_level": requested_level,
        "admin_name": name,
        "error": f"No Rwanda administrative boundary found for {name!r}.",
    }
