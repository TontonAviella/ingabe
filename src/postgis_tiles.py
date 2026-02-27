import logging
import os
import re

import asyncpg
from fastapi import HTTPException, status
from typing import List
from src.database.models import MapLayer
import redis.exceptions

logger = logging.getLogger(__name__)

# Strict whitelist: PostgreSQL identifiers must be [a-zA-Z_][a-zA-Z0-9_]*
# This prevents SQL injection via malicious column names.
_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")

# MVT source layer name — must match frontend tile source references
MVT_LAYER_NAME = "reprojectedfgb"

# MVT tile cache TTL in seconds (default: 5 minutes)
_MVT_CACHE_TTL = 300

# Lazy-initialized async Redis client for MVT caching
_async_redis = None


def _get_async_redis():
    """Return a lazily-initialized async Redis client for MVT tile caching.

    Returns None if Redis is unavailable or not configured.
    Binary mode (no decode_responses) for storing raw MVT bytes.
    """
    global _async_redis
    if _async_redis is None:
        try:
            from redis.asyncio import Redis as AsyncRedis
            _async_redis = AsyncRedis(
                host=os.environ.get("REDIS_HOST", "localhost"),
                port=int(os.environ.get("REDIS_PORT", 6379)),
                decode_responses=False,
            )
        except Exception as e:
            logger.warning("Failed to initialize Redis client for MVT caching: %s", e)
            return None
    return _async_redis


def _validate_column_name(name: str) -> str:
    """Validate and quote a PostgreSQL column identifier.

    Raises ValueError if the name doesn't match the safe identifier pattern.
    Uses quote_ident()-style double-quote wrapping as defense-in-depth.
    """
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid column name: {name!r}")
    # Double-quote to handle reserved words safely
    return f'"{name}"'


async def fetch_mvt_tile(
    layer: MapLayer, conn: asyncpg.Connection, z: int, x: int, y: int
) -> bytes:
    # Check if layer is a PostGIS type
    if layer.type != "postgis":
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

    # Validate every column name against whitelist before interpolation
    raw_names: List[str] = layer.postgis_attribute_column_list + ["id"]
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
        bounds_webmerc AS (
            SELECT ST_TileEnvelope($1, $2, $3) AS wm_geom
        ),
        bounds_4326 AS (
            SELECT ST_Transform(ST_TileEnvelope($1, $2, $3), 4326) AS geom_4326
        ),
        filtered AS (
            SELECT {", ".join([f"t.{name}" for name in safe_names])}, t.geom
            FROM ({layer.postgis_query}) t, bounds_4326 b
            WHERE t.geom && b.geom_4326
        ),
        candidates AS (
            SELECT {", ".join([f"f.{name}" for name in safe_names])},
                   {simplify_expr.replace('t.geom', 'f.geom')} AS geom
            FROM filtered f
        ),
        mvtgeom AS (
            SELECT {", ".join([f"c.{name}" for name in safe_names])},
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
