import logging
import re

import asyncpg
from fastapi import HTTPException, status
from collections import defaultdict
from typing import Dict, List, Optional, Sequence

from src.database.models import LAYER_TYPE_POSTGIS, MapLayer
import redis.exceptions

logger = logging.getLogger(__name__)

# Strict whitelist: PostgreSQL identifiers must be [a-zA-Z_][a-zA-Z0-9_]*
# This prevents SQL injection via malicious column names.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")

# MVT source layer name — must match frontend tile source references
MVT_LAYER_NAME = "reprojectedfgb"

# MVT tile cache TTL in seconds (default: 5 minutes)
_MVT_CACHE_TTL = 300

from src.dependencies.redis_client import get_async_redis as _get_async_redis


def _validate_column_name(name: str) -> str:
    """Validate and quote a PostgreSQL column identifier.

    Raises ValueError if the name doesn't match the safe identifier pattern.
    Uses quote_ident()-style double-quote wrapping as defense-in-depth.
    """
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid column name: {name!r}")
    # Double-quote to handle reserved words safely
    return f'"{name}"'


def _build_enrichment_cte(
    enrichments: Sequence,
) -> tuple[str, list[str]]:
    """Build a VALUES CTE and column list from layer_enrichments rows.

    Each row has (feature_id, column_name, value).

    Returns:
        (cte_sql, enrichment_col_names)  — e.g.
        ("enrichment_values AS (SELECT * FROM (VALUES (1,45.2,0.67), ...) AS ev(feature_id, cropland_pct, ndvi_mean))",
         ["cropland_pct", "ndvi_mean"])
    """
    # Pivot: group by feature_id, collect {col_name: value}
    by_feature: Dict[int, Dict[str, float]] = defaultdict(dict)
    col_names_set: dict[str, None] = {}  # ordered set
    for row in enrichments:
        fid = row["feature_id"]
        col = row["column_name"]
        val = row["value"]
        by_feature[fid][col] = val
        col_names_set[col] = None

    col_names = list(col_names_set.keys())

    # Validate all enrichment column names
    for cn in col_names:
        _validate_column_name(cn)

    # Build VALUES rows: (feature_id::int, v1::float8, v2::float8, ...)
    # First row uses explicit casts to set column types for PostgreSQL.
    val_rows = []
    is_first = True
    for fid, cols in by_feature.items():
        vals = []
        for cn in col_names:
            v = cols.get(cn)
            if v is not None:
                vals.append(f"{v}::float8" if is_first else str(v))
            else:
                vals.append("NULL::float8" if is_first else "NULL")
        fid_str = f"{fid}::int" if is_first else str(fid)
        val_rows.append(f"({fid_str},{','.join(vals)})")
        is_first = False

    values_sql = ",".join(val_rows)
    quoted_cols = ", ".join([_validate_column_name(cn) for cn in col_names])
    cte_sql = (
        f"enrichment_values AS ("
        f"SELECT * FROM (VALUES {values_sql}) "
        f'AS ev("feature_id", {quoted_cols})'
        f")"
    )

    return cte_sql, col_names


async def fetch_mvt_tile(
    layer: MapLayer,
    conn: asyncpg.Connection,
    z: int,
    x: int,
    y: int,
    enrichments: Optional[Sequence] = None,
) -> bytes:
    """Generate an MVT tile for a PostGIS layer, optionally injecting enrichment columns.

    When ``enrichments`` is provided (list of rows with feature_id, column_name, value),
    a VALUES CTE is prepended and LEFT JOINed so the enrichment columns appear in tiles.
    When ``enrichments`` is None, behaviour is identical to the original implementation.
    """
    # Check if layer is a PostGIS type
    if layer.type != LAYER_TYPE_POSTGIS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is not a PostGIS type. MVT tiles can only be generated from PostGIS data.",
        )

    if not layer.postgis_attribute_column_list:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PostGIS layer {layer.name} has no attribute columns, you must re-create the layer.",
        )

    # --- Redis cache check ---
    cache_key = f"mvt:{layer.layer_id}:{z}:{x}:{y}"
    redis_client = _get_async_redis()

    # Try to get cached tile
    if redis_client is not None:
        try:
            cached_tile = await redis_client.get(cache_key)
            if cached_tile is not None:
                logger.debug("MVT cache HIT for %s/%s/%s/%s", layer.layer_id, z, x, y)
                return cached_tile
        except redis.exceptions.RedisError as e:
            logger.warning("Redis error during MVT cache get for %s: %s", cache_key, e)
        except Exception as e:
            logger.error("Unexpected error during MVT cache get for %s: %s", cache_key, e)

    # --- Enrichment CTE (optional) ---
    enrich_cte = ""
    enrich_col_names: list[str] = []
    enrich_join = ""
    enrich_select_filtered = ""
    enrich_select_candidates = ""
    enrich_select_mvt = ""

    if enrichments:
        try:
            cte_sql, enrich_col_names = _build_enrichment_cte(enrichments)
            enrich_cte = cte_sql + ","
            enrich_join = ' LEFT JOIN enrichment_values e ON t."id" = e."feature_id"'
            enrich_select_filtered = ", " + ", ".join(
                [f'e.{_validate_column_name(cn)}' for cn in enrich_col_names]
            )
            enrich_select_candidates = ", " + ", ".join(
                [f'f.{_validate_column_name(cn)}' for cn in enrich_col_names]
            )
            enrich_select_mvt = ", " + ", ".join(
                [f'c.{_validate_column_name(cn)}' for cn in enrich_col_names]
            )
        except (ValueError, Exception) as e:
            logger.warning("Failed to build enrichment CTE, proceeding without: %s", e)
            enrich_cte = ""
            enrich_col_names = []
            enrich_join = ""
            enrich_select_filtered = ""
            enrich_select_candidates = ""
            enrich_select_mvt = ""

    # Build base column list from PostGIS source, excluding enrichment columns
    # (enrichment columns come from the CTE join, not the source table).
    enrich_col_set = set(enrich_col_names)
    raw_names: List[str] = [
        c for c in (layer.postgis_attribute_column_list or []) if c not in enrich_col_set
    ] + ["id"]
    try:
        safe_names = [_validate_column_name(n) for n in raw_names]
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PostGIS layer {layer.name} contains unsafe column name: {e}",
        )

    # At low zoom levels, simplify geometries to avoid query timeouts.
    # The tolerance is in Web Mercator metres — roughly 1 pixel worth.
    # At z7 a tile is ~1.2 km/px, at z10 ~150 m/px, at z14 ~10 m/px.
    if z <= 8:
        simplify_tolerance = 4096 * 20037508.34 * 2 / (4096 * (1 << z))  # ~1px in metres
        simplify_expr = f"ST_Simplify(ST_MakeValid(ST_Transform(t.geom, 3857)), {simplify_tolerance:.1f})"
    elif z <= 12:
        simplify_tolerance = 20037508.34 * 2 / (4096 * (1 << z))
        simplify_expr = f"ST_Simplify(ST_MakeValid(ST_Transform(t.geom, 3857)), {simplify_tolerance:.1f})"
    else:
        simplify_expr = "ST_MakeValid(ST_Transform(t.geom, 3857))"

    mvt_query = f"""
        WITH
        {enrich_cte}
        bounds_webmerc AS (
            SELECT ST_TileEnvelope($1, $2, $3) AS wm_geom
        ),
        bounds_4326 AS (
            SELECT ST_Transform(ST_TileEnvelope($1, $2, $3), 4326) AS geom_4326
        ),
        filtered AS (
            SELECT {", ".join([f"t.{name}" for name in safe_names])}{enrich_select_filtered}, t.geom
            FROM ({layer.postgis_query}) t{enrich_join}, bounds_4326 b
            WHERE t.geom && b.geom_4326
        ),
        candidates AS (
            SELECT {", ".join([f"f.{name}" for name in safe_names])}{enrich_select_candidates},
                   {simplify_expr.replace('t.geom', 'f.geom')} AS geom
            FROM filtered f
        ),
        mvtgeom AS (
            SELECT {", ".join([f"c.{name}" for name in safe_names])}{enrich_select_mvt},
                   ST_AsMVTGeom(c.geom, b.wm_geom::box2d) AS geom
            FROM candidates c, bounds_webmerc b
            WHERE c.geom IS NOT NULL
        )
        SELECT ST_AsMVT(mvtgeom, '{MVT_LAYER_NAME}', 4096, 'geom', 'id') FROM mvtgeom
        """

    # Set per-query timeout to prevent long-running tile queries
    # Wrap in explicit transaction so SET LOCAL is properly scoped
    try:
        async with conn.transaction():
            # Low zoom tiles process more features — allow more time
            timeout_ms = 20000 if z <= 10 else 10000
            await conn.execute(f"SET LOCAL statement_timeout = '{timeout_ms}'")
            result = await conn.fetchval(mvt_query, z, x, y)

        # --- Cache the result in Redis ---
        if redis_client is not None and result is not None:
            try:
                await redis_client.setex(cache_key, _MVT_CACHE_TTL, result)
                logger.debug("MVT cache MISS → cached %s/%s/%s/%s", layer.layer_id, z, x, y)
            except redis.exceptions.RedisError as e:
                logger.warning("Redis error during MVT cache write for %s: %s", cache_key, e)
            except Exception as e:
                logger.error("Unexpected error during MVT cache write for %s: %s", cache_key, e)

        return result
    except asyncpg.QueryCanceledError:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"Tile query timed out for layer {layer.name}. Consider adding a spatial index or simplifying the query.",
        )
