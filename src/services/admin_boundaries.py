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
        nearest, place_cols, places = await _nearest_parent_places(conn, table, parent_cols, given)
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


async def _nearest_parent_places(
    conn, table: str, parent_cols: tuple[str, ...], given: dict[str, str]
) -> tuple[str, tuple[str, ...], list]:
    """The places in `table` that the named parents pick out: the most specific named parent column,
    it and the columns above it, and their distinct values. More than one place means the name is shared."""
    where = " AND ".join(f"lower({col}) = lower(${i})" for i, col in enumerate(given, 1))
    nearest = next(col for col in parent_cols if col in given)
    place_cols = parent_cols[parent_cols.index(nearest):]
    places = await conn.fetch(
        f"SELECT DISTINCT {', '.join(place_cols)} FROM {table} WHERE {where}", *given.values()
    )
    return nearest, place_cols, places


async def _close_names(conn, table: str, name_col: str, value: str) -> list[str]:
    """Up to three real names in `table` closest to `value` (a misspelling)."""
    by_lower = {r[0].lower(): r[0] for r in await conn.fetch(f"SELECT DISTINCT {name_col} FROM {table}") if r[0]}
    return [by_lower[m] for m in difflib.get_close_matches(value.lower(), list(by_lower), n=3, cutoff=0.6)]


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
            close = await _close_names(conn, table, name_col, value)
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


# Levels a single unit can be shown at: (table, name column, parent columns nearest first).
_SHOW_SPEC = {"province": ("rwanda_province_boundaries", "province", ()), **_LIST_SPEC}
ALL_UNITS = frozenset({"*", "all"})
_SIZE_ORDER = ("village", "cell", "sector", "district", "province")


def _place(row, cols: tuple[str, ...]) -> dict[str, str]:
    """{"name": ..., "district": ..., ...} for a row of `cols` (name column first)."""
    return {"name": row[cols[0]], **{c.removesuffix("_name"): row[c] for c in cols[1:]}}


async def resolve_admin_boundary(
    conn,
    args: dict[str, object],
) -> dict[str, object]:
    """Resolve a request to show one Rwanda admin unit, or every unit of a level inside one parent.

    `args`: admin_level (province|district|sector|cell|village|auto), name ("*" or "all" for every
    unit), and optional district / sector / cell parents. Only what was asked is delivered:

    - "success": exactly one unit (or the units inside exactly one parent place), with the PostGIS
      `query` that draws it, its `bounds` and `within` (its parents);
    - "ambiguous": the name, or the parent of "all units in", is several places; no query, the
      `candidates` say where each one is;
    - "not_found": no such unit (where the parents say); `error` says why, with close spellings
      or where units of that name really are;
    - "error": a missing name or an unknown level.
    """
    requested_level = str(args.get("admin_level") or "auto").strip().lower()
    name = str(args.get("name") or "").strip()
    if not name:
        return {"status": "error", "error": "Missing boundary name."}
    given = {
        f"{key}_name": str(args[key]).strip()
        for key in ("district", "sector", "cell")
        if args.get(key) and str(args[key]).strip()
    }
    if requested_level == "auto":
        # A unit named "in" a parent is smaller than that parent: "Murambi in Rangiro sector" is a cell or village.
        smallest_parent = min((_SIZE_ORDER.index(col.removesuffix("_name")) for col in given), default=len(_SIZE_ORDER))
        levels = [level for level in _SHOW_SPEC if _SIZE_ORDER.index(level) < smallest_parent]
    else:
        levels = [requested_level]
    if not levels or not all(level in _SHOW_SPEC for level in levels):
        return {"status": "error", "error": f"Unknown admin level {requested_level!r}."}

    for level in levels:
        result = await _resolve_at_level(conn, level, name, given)
        if result["status"] != "not_found":
            return result
    return {
        "status": "not_found",
        "admin_level": requested_level,
        "admin_name": name,
        "error": await _not_found_message(conn, levels, name, given),
    }


async def _resolve_at_level(conn, level: str, name: str, given: dict[str, str]) -> dict[str, object]:
    table, name_col, parent_cols = _SHOW_SPEC[level]
    cols = (name_col, *parent_cols)
    parents = {col: value for col, value in given.items() if col in parent_cols}
    all_units = name.lower() in ALL_UNITS

    if all_units and parents:
        # "the cells of Busasamana": the parent must be one place, not every place of that name.
        nearest, place_cols, places = await _nearest_parent_places(conn, table, parent_cols, parents)
        if not places:
            return {"status": "not_found", "admin_level": level}
        if len(places) > 1:
            parent_level = nearest.removesuffix("_name")
            return {
                "status": "ambiguous",
                "admin_level": parent_level,
                "admin_name": places[0][nearest],
                "units_of": level,
                "match_count": len(places),
                "candidates": [_place(row, place_cols) for row in places],
            }

    filters: list[str] = []
    params: list[object] = []
    if not all_units:
        params.append(name)
        filters.append(f"lower({name_col}) = lower(${len(params)})")
    for col, value in parents.items():
        params.append(value)
        filters.append(f"lower({col}) = lower(${len(params)})")
    where = " AND ".join(filters) or "TRUE"
    try:
        rows = await conn.fetch(
            f"""
            SELECT {', '.join(cols)},
                   ST_XMin(ST_Extent(geom)) AS xmin, ST_YMin(ST_Extent(geom)) AS ymin,
                   ST_XMax(ST_Extent(geom)) AS xmax, ST_YMax(ST_Extent(geom)) AS ymax,
                   COUNT(*) OVER() AS match_count
            FROM {table}
            WHERE {where}
            GROUP BY {', '.join(cols)}
            ORDER BY {', '.join(cols)}
            LIMIT 12
            """,
            *params,
        )
    except asyncpg.exceptions.UndefinedTableError:
        logger.warning("Admin boundary table missing: %s", table)
        return {"status": "not_found", "admin_level": level}
    if not rows:
        return {"status": "not_found", "admin_level": level}
    total = int(rows[0]["match_count"])

    if not all_units and total > 1:
        # Several units share the name: deliver none of them and say where each one is.
        return {
            "status": "ambiguous",
            "admin_level": level,
            "admin_name": rows[0][name_col],
            "given": {col.removesuffix("_name"): value for col, value in parents.items()},
            "match_count": total,
            "candidates": [_place(row, cols) for row in rows],
        }

    # The layer stores its query as SQL text, so values are inlined as escaped literals.
    sql_filters = [
        f"LOWER({col}) = LOWER({_admin_sql_literal(value)})"
        for col, value in ([(name_col, name)] if not all_units else []) + list(parents.items())
    ]
    query = (
        f"SELECT ROW_NUMBER() OVER()::bigint AS id, {', '.join(cols)}, geom "
        f"FROM {table} WHERE {' AND '.join(sql_filters) or 'TRUE'}"
    )
    if all_units:
        extent = await conn.fetchrow(
            f"""
            SELECT ST_XMin(ST_Extent(geom)) AS xmin, ST_YMin(ST_Extent(geom)) AS ymin,
                   ST_XMax(ST_Extent(geom)) AS xmax, ST_YMax(ST_Extent(geom)) AS ymax
            FROM {table} WHERE {where}
            """,
            *params,
        )
    else:
        extent = rows[0]
    bounds = [float(extent["xmin"]), float(extent["ymin"]), float(extent["xmax"]), float(extent["ymax"])]
    display_name = f"{level.title()} Boundaries" if all_units else str(rows[0][name_col])
    within = (
        {col.removesuffix("_name"): value for col, value in parents.items()}
        if all_units
        else {k: v for k, v in _place(rows[0], cols).items() if k != "name"}
    )
    return {
        "status": "success",
        "admin_level": level,
        "admin_name": display_name,
        "within": within,
        "query": query,
        "bounds": bounds,
        "feature_count": total if all_units else 1,
        "attribute_columns": list(cols),
        "layer_name": display_name if all_units else f"{display_name} {level.title()} Boundary",
    }


def _parents_text(row, parent_cols: tuple[str, ...]) -> str:
    """"Gatare sector, Nyamagabe district" for a row's parent columns."""
    return ", ".join(f"{row[col]} {col.removesuffix('_name')}" for col in parent_cols)


async def _not_found_message(conn, levels: list[str], name: str, given: dict[str, str]) -> str:
    """Why nothing matched: a named parent that does not exist, the places a unit of that name
    really is in, or the closest real names."""
    level_text = " or ".join(levels) if len(levels) < 3 else ", ".join(levels[:-1]) + " or " + levels[-1]
    parents = {col: value for col, value in given.items() if col.removesuffix("_name") in _LIST_SPEC}
    if parents:
        for col, value in parents.items():
            table, name_col, _ = _LIST_SPEC[col.removesuffix("_name")]
            if not await conn.fetchval(f"SELECT 1 FROM {table} WHERE lower({name_col}) = lower($1) LIMIT 1", value):
                close = await _close_names(conn, table, name_col, value)
                hint = f" Did you mean {', '.join(close)}?" if close else ""
                return f"There is no {col.removesuffix('_name')} named {value!r} in Rwanda.{hint}"
        if name.lower() not in ALL_UNITS:
            where_text = ", ".join(f"{value} {col.removesuffix('_name')}" for col, value in parents.items())
            elsewhere: list[str] = []
            for level in levels:
                table, name_col, parent_cols = _SHOW_SPEC[level]
                if not parent_cols:
                    continue
                rows = await conn.fetch(
                    f"SELECT DISTINCT {name_col}, {', '.join(parent_cols)} FROM {table} "
                    f"WHERE lower({name_col}) = lower($1) ORDER BY {', '.join(parent_cols)} LIMIT 6",
                    name,
                )
                elsewhere += [f"{row[name_col]} {level} ({_parents_text(row, parent_cols)})" for row in rows]
            found = f" Elsewhere: {'; '.join(elsewhere[:6])}{'; ...' if len(elsewhere) > 6 else ''}." if elsewhere else ""
            return f"There is no {level_text} named {name!r} in {where_text}.{found}"
    close: list[str] = []
    for level in levels:
        table, name_col, _ = _SHOW_SPEC[level]
        close += [n for n in await _close_names(conn, table, name_col, name) if n not in close]
    hint = f" Did you mean {', '.join(close[:3])}?" if close else ""
    return f"There is no Rwanda {level_text} named {name!r}.{hint}"
