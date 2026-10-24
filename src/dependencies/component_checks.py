"""Component self-tests: every part the live app depends on is asked to do its job, with a known answer.

Up/down probes say a service answers; these say it does what the app needs (PostGIS returns Rwanda's 30 districts
with the right area, GDAL reads a cloud-optimized GeoTIFF window, rasterd renders a tile, Ollama returns 768-number
embeddings, the STAC catalogues find recent scenes...). One app worker runs them in the background (a Redis lock
picks it), each on its own interval and time limit; results are kept in Redis and exported on /metrics as
component_check_ok / _duration_seconds / _last_run_timestamp_seconds with labels component, check and tier
("core": the live app breaks without it; "feed": an outside data source; "ai": the AI model and embeddings).

Checks only read, except two that clean up after themselves: a MinIO object under snapshots/selftest/ (deleted at
once; that prefix also expires in a day) and a Redis key with a 60 s life. The AI-model check is one tiny call an
hour. A failed check is tried again after RETRY_AFTER_S, and only two failures in a row export ok=0, so one blip
raises no alarm. At most MAX_PARALLEL checks run at once, on their own threads, never the app's shared pool.
Alerts: ComponentCheckFailing and ComponentChecksNotRunning in monitoring/alerts.yml; dashboard "Components".
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Awaitable, Callable

from src.dependencies.redis_client import get_async_redis

logger = logging.getLogger(__name__)

_KEY = "component_checks:v1"  # Redis hash: check name -> JSON result
_LOCK = "component_checks:runner"  # one runner across app workers
RETRY_AFTER_S = 120  # a failed check runs again this soon, to tell a blip from a fault
MAX_PARALLEL = 3
_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL, thread_name_prefix="component-check")
_SLOTS: asyncio.Semaphore | None = None
UA = {"User-Agent": "ingabe-selftest (gis.nozalabs.rw)"}

# The stored district outlines (geoBoundaries) cover 25,316 km² (PostGIS and GeoPandas agreed, 2026-10-25); Rwanda's
# official 26,338 km² includes lakes the outlines leave out. A change of more than 2% means the outlines changed.
RWANDA_KM2, AREA_TOLERANCE = 25_316, 0.02
KIGALI = (30.06, -1.95)
# A stable public cloud-optimized GeoTIFF (iSDAsoil topsoil pH, 30 m, Africa-wide), also on the app's allow-list.
PUBLIC_COG = "https://isdasoil.s3.amazonaws.com/soil_data/ph/ph.tif"


class CheckFailed(Exception):
    """The component answered, but not with what the app needs."""


@dataclass(frozen=True)
class Check:
    name: str
    component: str
    tier: str  # core | feed | ai
    every_s: int
    timeout_s: float
    run: Callable[[], Awaitable[str]]  # returns a short detail; raises when the component is not doing its job


async def _in_thread(fn, *args):
    """Run blocking work on the checks' own threads (a hung check cannot take the app's shared pool)."""
    return await asyncio.get_running_loop().run_in_executor(_POOL, functools.partial(fn, *args))


def _http(url: str, method: str = "GET", body: bytes | None = None, headers: dict | None = None,
          timeout: float = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read() if method != "HEAD" else b""
    except urllib.error.HTTPError as e:
        return e.code, b""


async def _get_json(url: str, timeout: float = 15) -> Any:
    status, body = await _in_thread(_http, url, "GET", None, None, timeout)
    if status != 200:
        raise CheckFailed(f"HTTP {status} from {url.split('?')[0]}")
    return json.loads(body)


# --- core ------------------------------------------------------------------------------------------------------

async def check_postgis() -> str:
    from src.database.pool import get_async_db_connection

    async with get_async_db_connection() as conn:
        n, km2, version = await conn.fetchrow(
            "SELECT count(*), sum(ST_Area(geom::geography)) / 1e6, PostGIS_Lib_Version() FROM rwanda_district_boundaries")
    if n != 30:
        raise CheckFailed(f"{n} districts, expected 30")
    if abs(km2 - RWANDA_KM2) > RWANDA_KM2 * AREA_TOLERANCE:
        raise CheckFailed(f"districts cover {km2:,.0f} km², expected about {RWANDA_KM2:,}")
    return f"30 districts, {km2:,.0f} km², PostGIS {version}"


async def check_geopandas() -> str:
    """GeoPandas + Shapely + pyproj (the vector upload path) agree with PostGIS on Rwanda's area."""
    from src.database.pool import get_async_db_connection

    async with get_async_db_connection() as conn:
        rows = await conn.fetch("SELECT district, ST_AsBinary(geom) AS wkb FROM rwanda_district_boundaries")

    def area() -> float:
        import geopandas as gpd
        from shapely import wkb

        gdf = gpd.GeoDataFrame({"district": [r["district"] for r in rows]},
                               geometry=[wkb.loads(bytes(r["wkb"])) for r in rows], crs="EPSG:4326")
        return float(gdf.to_crs("EPSG:6933").area.sum() / 1e6)  # equal-area projection

    km2 = await _in_thread(area)
    if abs(km2 - RWANDA_KM2) > RWANDA_KM2 * AREA_TOLERANCE:
        raise CheckFailed(f"GeoPandas measures {km2:,.0f} km², expected about {RWANDA_KM2:,}")
    return f"{len(rows)} districts, {km2:,.0f} km² (equal-area)"


async def check_redis() -> str:
    r = get_async_redis()
    if r is None:
        raise CheckFailed("no Redis client")
    key, value = f"selftest:{time.time_ns()}", str(time.time_ns())
    await r.set(key, value, ex=60)
    got = await r.get(key)
    await r.delete(key)
    if (got.decode() if isinstance(got, bytes) else got) != value:
        raise CheckFailed("read back a different value")
    return "write, read, delete"


async def check_minio() -> str:
    from src.utils import get_async_s3_client, get_bucket_name

    bucket, key, payload = get_bucket_name(), f"snapshots/selftest/{time.time_ns()}.txt", os.urandom(32).hex()
    s3 = await get_async_s3_client()  # the app's shared, already-open client: used, never closed here
    try:
        await s3.put_object(Bucket=bucket, Key=key, Body=payload.encode())
        got = await (await s3.get_object(Bucket=bucket, Key=key))["Body"].read()
    finally:
        await s3.delete_object(Bucket=bucket, Key=key)
    if got.decode() != payload:
        raise CheckFailed("read back a different object")
    return f"write, read, delete in {bucket}"


def _gdal_drivers() -> str:
    version = subprocess.run(["gdalinfo", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
    raster = subprocess.run(["gdalinfo", "--formats"], capture_output=True, text=True, timeout=10).stdout
    vector = subprocess.run(["ogrinfo", "--formats"], capture_output=True, text=True, timeout=10).stdout
    # What the app uses GDAL for (uploads, conversions, tiles). GeoParquet goes through GeoPandas and DuckDB, not GDAL.
    need_raster, need_vector = ["GTiff", "COG", "PNG"], ["GPKG", "FlatGeobuf", "GeoJSON", "PMTiles", "MVT"]
    missing = [d for d in need_raster if not re.search(rf"^\s*{d}\b", raster, re.M)]
    missing += [d for d in need_vector if not re.search(rf"^\s*{d}\b", vector, re.M)]
    if missing:
        raise CheckFailed(f"GDAL lacks {', '.join(missing)}")
    with tempfile.TemporaryDirectory() as tmp:  # make a COG and check it is one
        src, cog = os.path.join(tmp, "in.tif"), os.path.join(tmp, "out.tif")
        subprocess.run(["gdal_create", "-of", "GTiff", "-outsize", "512", "512", "-bands", "1", "-burn", "7",
                        "-a_srs", "EPSG:4326", "-a_ullr", "29", "-1", "31", "-3", src], check=True, capture_output=True,
                       timeout=20)
        subprocess.run(["gdal_translate", "-of", "COG", src, cog], check=True, capture_output=True, timeout=20)
        info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", cog], capture_output=True, text=True,
                                         timeout=20).stdout)
    layout = info.get("metadata", {}).get("IMAGE_STRUCTURE", {}).get("LAYOUT")
    mean = info["bands"][0].get("mean")
    if layout != "COG" or mean != 7:
        raise CheckFailed(f"made a file with layout {layout} and mean {mean}, expected a COG of 7s")
    return f"{version.split(',')[0]}; drivers present; wrote a valid COG"


async def check_gdal() -> str:
    return await _in_thread(_gdal_drivers)


def _read_public_cog() -> str:
    import rasterio
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    with rasterio.Env(GDAL_HTTP_TIMEOUT="20", GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR"), rasterio.open(PUBLIC_COG) as src:
        if not src.profile.get("tiled") or not src.overviews(1):
            raise CheckFailed("the COG is not tiled or has no overviews")
        x, y = KIGALI  # the COG is in its own projection: move the box over Kigali into it
        win = from_bounds(*transform_bounds("EPSG:4326", src.crs, x - 0.01, y - 0.01, x + 0.01, y + 0.01), src.transform)
        band = src.read(1, window=win)
    valid = band[(band > 0) & (band != src.nodata)] if src.nodata is not None else band[band > 0]
    if valid.size == 0:
        raise CheckFailed("no valid pixels near Kigali")
    ph = float(valid.mean()) / 10  # stored as pH x 10
    if not 3.5 <= ph <= 8.5:
        raise CheckFailed(f"soil pH near Kigali reads {ph:.1f}, outside 3.5-8.5")
    return f"read a window over Kigali over HTTP: soil pH {ph:.1f} ({valid.size} pixels)"


async def check_rasterio_cog() -> str:
    return await _in_thread(_read_public_cog)


def _tile_xy(lon: float, lat: float, z: int) -> tuple[int, int]:
    import math

    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)
    return x, y


async def check_rasterd() -> str:
    """The Rust/GDAL tile server renders a real PNG of the public COG over Kigali (what map raster layers use)."""
    base = os.environ.get("RASTER_TILE_ENGINE_URL", "http://rasterd:8877").rstrip("/")
    status, _ = await _in_thread(_http, f"{base}/healthz", "GET", None, None, 5)
    if status != 200:
        raise CheckFailed(f"healthz HTTP {status}")
    z = 12
    x, y = _tile_xy(*KIGALI, z)
    url = f"{base}/tiles/{z}/{x}/{y}.png?" + urllib.parse.urlencode({"url": PUBLIC_COG})
    status, body = await _in_thread(_http, url, "GET", None, None, 25)
    if status != 200 or not body.startswith(b"\x89PNG") or len(body) < 200:
        raise CheckFailed(f"tile HTTP {status}, {len(body)} bytes")
    return f"rendered tile {z}/{x}/{y} ({len(body):,} bytes)"


async def check_map_tiles() -> str:
    """The app's own tile route, as MapLibre calls it. The tile (over Kampala) is outside the region the app keeps
    in S3, so each check fetches it from the upstream terrain tiles and stores nothing."""
    x, y = _tile_xy(32.58, 0.32, 8)
    port = os.environ.get("PORT", "8000")
    status, body = await _in_thread(_http, f"http://127.0.0.1:{port}/api/basemaps/terrain/8/{x}/{y}.png",
                                           "GET", None, None, 20)
    if status != 200 or not body.startswith(b"\x89PNG"):
        raise CheckFailed(f"terrain tile HTTP {status}, {len(body)} bytes")
    return f"terrain tile served ({len(body):,} bytes)"


def _duckdb_geoparquet() -> str:
    """GeoPandas writes GeoParquet (as vector uploads do) and DuckDB reads it back (as the layer describer does)."""
    import duckdb
    import geopandas as gpd
    from shapely.geometry import box

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "t.parquet")
        gpd.GeoDataFrame({"n": [1, 2, 3]}, geometry=[box(i, 0, i + 1, 1) for i in range(3)],
                         crs="EPSG:4326").to_parquet(path)
        con = duckdb.connect(":memory:")
        n, total = con.execute(f"SELECT count(*), sum(n) FROM read_parquet('{path}')").fetchone()
        con.close()
    if (n, total) != (3, 6):
        raise CheckFailed(f"read back {n} rows summing {total}, expected 3 and 6")
    return "GeoParquet written by GeoPandas, read by DuckDB"


async def check_duckdb() -> str:
    return await _in_thread(_duckdb_geoparquet)


def _dask_sum() -> str:
    import dask.array as da

    total = int(da.ones((2000, 2000), chunks=(500, 500)).sum().compute(scheduler="threads"))
    if total != 4_000_000:
        raise CheckFailed(f"summed {total}, expected 4,000,000")
    return "16-chunk array summed correctly"


async def check_dask() -> str:
    return await _in_thread(_dask_sum)


# --- AI ---------------------------------------------------------------------------------------------------------

async def check_ollama_embeddings() -> str:
    base = (os.environ.get("BRAIN_EMBEDDINGS_BASE_URL") or os.environ.get("OLLAMA_BASE_URL") or "http://ollama:11434")
    base = base.rstrip("/").removesuffix("/v1")
    body = json.dumps({"model": "nomic-embed-text", "prompt": "Kigali maize field", "keep_alive": "30m"}).encode()
    status, raw = await _in_thread(_http, f"{base}/api/embeddings", "POST", body,
                                          {"Content-Type": "application/json"}, 30)
    if status != 200:
        raise CheckFailed(f"HTTP {status}")
    dims = len(json.loads(raw).get("embedding", []))
    if dims != 768:
        raise CheckFailed(f"{dims}-number embedding, expected 768")
    return "768-number embedding"


async def check_ai_model() -> str:
    """One tiny call to Sage's model: it must answer, and with the word asked for."""
    from src.utils import get_chat_client_for_model

    client, model = get_chat_client_for_model(None)  # type: ignore[arg-type]  # the request is not used
    try:
        # 200 tokens: reasoning models spend tokens thinking first. No temperature: Claude Haiku 5.5 refuses it.
        resp = await client.chat.completions.create(
            model=model, max_tokens=200, messages=[{"role": "user", "content": "Reply with exactly the word: READY"}])
    finally:
        await client.close()
    choice = resp.choices[0]
    text = (choice.message.content or "").strip()
    if choice.finish_reason == "stop" and "READY" not in text.upper():
        raise CheckFailed(f"answered {text[:40]!r} instead of READY")
    return f"{model} answered"


# --- outside data feeds -----------------------------------------------------------------------------------------

async def check_deafrica_stac() -> str:
    x, y = KIGALI
    since = (date.today() - timedelta(days=20)).isoformat()
    found = await _get_json("https://explorer.digitalearth.africa/stac/search?" + urllib.parse.urlencode({
        "collections": "s2_l2a", "bbox": f"{x - .05},{y - .05},{x + .05},{y + .05}", "limit": 5,
        "datetime": f"{since}T00:00:00Z/..",
    }), 30)
    n = len(found.get("features", []))
    if n == 0:
        raise CheckFailed("no Sentinel-2 scene over Kigali in 20 days (Sentinel-2 passes every 5)")
    return f"{n} Sentinel-2 scenes over Kigali in 20 days"


async def check_planetary_computer() -> str:
    col = await _get_json("https://planetarycomputer.microsoft.com/api/stac/v1/collections/modis-11A2-061")
    token = await _get_json("https://planetarycomputer.microsoft.com/api/sas/v1/token/modiseuwest/modis-061-cogs")
    if col.get("id") != "modis-11A2-061" or not token.get("token"):
        raise CheckFailed("collection or storage token missing")
    return "MODIS collection and storage token"


async def check_earth_search() -> str:
    col = await _get_json("https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a")
    if col.get("id") != "sentinel-2-l2a":
        raise CheckFailed("collection missing")
    return "Sentinel-2 collection"


async def check_chirps() -> str:
    day = date.today() - timedelta(days=60)  # the final product lags three weeks or more
    url = (f"https://data.chc.ucsb.edu/products/CHIRPS-2.0/africa_daily/tifs/p05/{day:%Y}/"
           f"chirps-v2.0.{day:%Y.%m.%d}.tif.gz")
    status, _ = await _in_thread(_http, url, "HEAD", None, None, 20)
    if status != 200:
        raise CheckFailed(f"HTTP {status} for the {day} file")
    return f"daily rain file for {day} is there"


async def check_open_meteo() -> str:
    x, y = KIGALI
    found = await _get_json(f"https://api.open-meteo.com/v1/forecast?latitude={y}&longitude={x}"
                            "&daily=precipitation_sum&forecast_days=3&timezone=Africa%2FKigali")
    rain = found.get("daily", {}).get("precipitation_sum") or []
    if len(rain) != 3:
        raise CheckFailed("no 3-day rain forecast")
    return "3-day rain forecast for Kigali"


async def check_wapor() -> str:
    found = await _get_json("https://data.apps.fao.org/gismgr/api/v2/catalog/workspaces/WAPOR-3/mapsets/L2-AETI-D", 30)
    if (found.get("response") or {}).get("code") != "L2-AETI-D":
        raise CheckFailed("WaPOR catalog does not list L2-AETI-D")
    return "WaPOR evapotranspiration mapset listed"


async def check_workos() -> str:
    client_id = os.environ.get("WORKOS_CLIENT_ID", "")
    if os.environ.get("AUTH_PROVIDER", "").lower() != "workos" or not client_id:
        return "WorkOS sign-in not in use here"
    keys = (await _get_json(f"https://api.workos.com/sso/jwks/{client_id}")).get("keys", [])
    if not keys:
        raise CheckFailed("no signing keys for our client")
    return f"{len(keys)} signing key(s) for our client"


CHECKS: tuple[Check, ...] = (
    Check("postgis", "PostGIS", "core", 300, 15, check_postgis),
    Check("geopandas", "GeoPandas", "core", 900, 30, check_geopandas),
    Check("redis", "Redis", "core", 300, 5, check_redis),
    Check("minio", "MinIO storage", "core", 300, 15, check_minio),
    Check("gdal", "GDAL", "core", 3600, 60, check_gdal),
    Check("rasterio_cog", "rasterio + COG", "core", 900, 45, check_rasterio_cog),
    Check("rasterd", "Raster tile server", "core", 900, 45, check_rasterd),
    Check("map_tiles", "Map tiles (MapLibre)", "core", 900, 30, check_map_tiles),
    Check("duckdb", "DuckDB + GeoParquet", "core", 900, 30, check_duckdb),
    Check("dask", "Dask", "core", 1800, 30, check_dask),
    Check("ollama_embeddings", "Ollama embeddings", "ai", 600, 45, check_ollama_embeddings),
    Check("ai_model", "AI model", "ai", 3600, 45, check_ai_model),
    Check("deafrica_stac", "Digital Earth Africa STAC", "feed", 1800, 40, check_deafrica_stac),
    Check("planetary_computer", "Planetary Computer", "feed", 1800, 30, check_planetary_computer),
    Check("earth_search", "Earth Search STAC", "feed", 3600, 30, check_earth_search),
    Check("chirps", "CHIRPS rain", "feed", 3600, 30, check_chirps),
    Check("open_meteo", "Open-Meteo forecast", "feed", 1800, 30, check_open_meteo),
    Check("wapor", "WaPOR", "feed", 3600, 40, check_wapor),
    Check("workos", "WorkOS sign-in", "core", 900, 20, check_workos),
)


# --- running and exporting --------------------------------------------------------------------------------------

async def run_one(check: Check, fails_before: int = 0) -> dict[str, Any]:
    global _SLOTS
    if _SLOTS is None:
        _SLOTS = asyncio.Semaphore(MAX_PARALLEL)
    async with _SLOTS:  # the time limit starts once the check runs, not while it waits for a slot
        return await _run(check, fails_before)


async def _run(check: Check, fails_before: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        detail, ok = await asyncio.wait_for(check.run(), check.timeout_s), 1
    except asyncio.TimeoutError:
        detail, ok = f"no answer within {check.timeout_s:.0f} s", 0
    except CheckFailed as e:
        detail, ok = str(e), 0
    except Exception as e:  # noqa: BLE001 - any error is the component failing its check; recorded and logged
        detail, ok = f"{type(e).__name__}: {str(e)[:200]}", 0
    result = {"ok": ok, "fails": 0 if ok else fails_before + 1, "detail": detail,
              "seconds": round(time.monotonic() - started, 3), "at": time.time(),
              "component": check.component, "tier": check.tier}
    if not ok:
        logger.warning("component check %s failed: %s", check.name, detail)
    return result


async def run_due(now: float | None = None) -> list[str]:
    """Run every check whose interval has passed; returns the names run."""
    r = get_async_redis()
    if r is None:
        return []
    now = now or time.time()
    stored = {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in (await r.hgetall(_KEY)).items()}
    def is_due(c: Check) -> bool:
        last = stored.get(c.name, {})
        wait = RETRY_AFTER_S if last.get("fails") else c.every_s
        return now - last.get("at", 0) >= wait

    due = [c for c in CHECKS if is_due(c)]
    results = await asyncio.gather(*(run_one(c, stored.get(c.name, {}).get("fails", 0)) for c in due))
    if due:
        await r.hset(_KEY, mapping={c.name: json.dumps(res) for c, res in zip(due, results)})
    return [c.name for c in due]


async def runner_loop() -> None:
    """Background loop: every 30 s the worker holding the Redis lock runs what is due."""
    await asyncio.sleep(20)  # let the app finish starting
    me = f"{os.getpid()}:{time.time_ns()}"
    while True:
        try:
            r = get_async_redis()
            if r is not None:
                mine = await r.set(_LOCK, me, ex=120, nx=True)
                if not mine and (await r.get(_LOCK)) in (me.encode(), me):
                    mine = await r.expire(_LOCK, 120)
                if mine:
                    await run_due()
        except Exception:  # noqa: BLE001 - the loop must survive anything; the stale alert covers a dead runner
            logger.warning("component checks cycle failed", exc_info=True)
        await asyncio.sleep(30)


async def metric_lines() -> list[str]:
    r = get_async_redis()
    if r is None:
        return []
    try:
        stored = await asyncio.wait_for(r.hgetall(_KEY), 1.0)
    except Exception:  # noqa: BLE001
        logger.warning("component check results could not be read", exc_info=True)
        return []
    lines = {"ok": [], "duration_seconds": [], "last_run_timestamp_seconds": []}
    by_name = {c.name: c for c in CHECKS}
    for k, v in sorted(stored.items()):
        name = k.decode() if isinstance(k, bytes) else k
        if name not in by_name:
            continue
        res, c = json.loads(v), by_name[name]
        lab = f'check="{name}",component="{c.component}",tier="{c.tier}"'
        # One failure is retried before it counts: ok=0 only after two failures in a row.
        lines["ok"].append(f"component_check_ok{{{lab}}} {0 if res.get('fails', 0) >= 2 else 1}")
        lines["duration_seconds"].append(f"component_check_duration_seconds{{{lab}}} {res['seconds']}")
        lines["last_run_timestamp_seconds"].append(f"component_check_last_run_timestamp_seconds{{{lab}}} {res['at']:.0f}")
    help_ = {"ok": ("gauge", "1 when the component did its job at the last check"),
             "duration_seconds": ("gauge", "How long the last check took"),
             "last_run_timestamp_seconds": ("gauge", "When the check last ran")}
    out: list[str] = []
    for suffix, rows in lines.items():
        if rows:
            out += [f"# HELP component_check_{suffix} {help_[suffix][1]}", f"# TYPE component_check_{suffix} {help_[suffix][0]}", *rows]
    return out


async def results() -> dict[str, dict[str, Any]]:
    """The latest result of every check, with its detail (for the internal status endpoint)."""
    r = get_async_redis()
    if r is None:
        return {}
    stored = await r.hgetall(_KEY)
    return {(k.decode() if isinstance(k, bytes) else k): json.loads(v) for k, v in stored.items()}
