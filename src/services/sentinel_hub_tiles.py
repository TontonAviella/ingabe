"""Sentinel Hub Process API tile proxy — OAuth2 token management and tile fetching.

Proxies tile requests to Sentinel Hub Process API, keeping OAuth2 client
credentials server-side. Sends evalscripts directly per request — no
SH_INSTANCE_ID or Configuration API setup needed.

Supports PlanetScope (via BYOC), Sentinel-2 L2A, and SkySat collections.

Environment variables (via src.config.settings):
    SH_CLIENT_ID:      Sentinel Hub OAuth client ID
    SH_CLIENT_SECRET:  Sentinel Hub OAuth client secret
"""

import logging
import math
import os
import time
from typing import Optional

import aiohttp

from src.config import settings

logger = logging.getLogger(__name__)

# Shared aiohttp session — reuses TCP+TLS connections across tile requests.
# Created lazily on first use, avoids per-request connection overhead.
_shared_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    """Return a shared aiohttp session, creating it if needed."""
    global _shared_session
    if _shared_session is None or _shared_session.closed:
        _shared_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30),
            connector=aiohttp.TCPConnector(limit=20, keepalive_timeout=60),
        )
    return _shared_session

# Sentinel Hub OAuth2 token endpoint — respects SH_BASE_URL for CDSE deployments
_SH_BASE_URL = settings.sh_base_url if hasattr(settings, "sh_base_url") else (
    os.environ.get("SH_BASE_URL", "https://services.sentinel-hub.com")
)
if "dataspace.copernicus.eu" in _SH_BASE_URL:
    _SH_TOKEN_URL = (
        "https://identity.dataspace.copernicus.eu/auth/realms/"
        "CDSE/protocol/openid-connect/token"
    )
    _SH_PROCESS_URL = f"{_SH_BASE_URL}/api/v1/process"
    _SH_CATALOG_URL = f"{_SH_BASE_URL}/api/v1/catalog/1.0.0/search"
else:
    _SH_TOKEN_URL = (
        "https://services.sentinel-hub.com/auth/realms/main/"
        "protocol/openid-connect/token"
    )
    _SH_PROCESS_URL = "https://services.sentinel-hub.com/api/v1/process"
    _SH_CATALOG_URL = "https://services.sentinel-hub.com/api/v1/catalog/1.0.0/search"

# Cached token state
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0


def is_configured() -> bool:
    """Check if Sentinel Hub tile proxy credentials are set."""
    return bool(settings.sh_client_id and settings.sh_client_secret)


async def get_access_token(force_refresh: bool = False) -> str:
    """Return a valid OAuth2 access token, fetching a new one if expired.

    Tokens are cached in memory with a 5-minute safety margin before expiry.
    Set ``force_refresh=True`` to bypass the cache after a 401 from the API
    (server-side revocation, credential rotation, or clock drift can invalidate
    a token before its advertised expiry).
    """
    global _cached_token, _token_expires_at

    if not force_refresh and _cached_token and time.monotonic() < _token_expires_at:
        return _cached_token

    if not is_configured():
        raise RuntimeError("Sentinel Hub credentials not configured")

    session = _get_session()
    async with session.post(
        _SH_TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": settings.sh_client_id,
            "client_secret": settings.sh_client_secret,
        },
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise RuntimeError(
                f"Sentinel Hub token request failed ({resp.status}): {body}"
            )
        data = await resp.json()

    _cached_token = data["access_token"]
    expires_in = data.get("expires_in", 3600)
    # Refresh 5 minutes before expiry
    _token_expires_at = time.monotonic() + expires_in - 300
    logger.info("Sentinel Hub access token refreshed (expires_in=%ds)", expires_in)
    return _cached_token


async def search_catalog(
    bbox_wgs84: tuple[float, float, float, float],
    *,
    collection: str = "sentinel-2-l2a",
    date_from: str = "",
    date_to: str = "",
) -> list[dict]:
    """Search Sentinel Hub Catalog for scenes covering a WGS84 bounding box.

    Returns a list of scene dicts with 'datetime' and 'cloud_cover' fields,
    sorted by cloud cover ascending (clearest first).
    """
    search_body: dict = {
        "collections": [collection],
        "bbox": list(bbox_wgs84),
        "limit": 5,
        "fields": {
            "include": [
                "properties.datetime",
                "properties.eo:cloud_cover",
            ],
        },
    }
    if date_from and date_to:
        search_body["datetime"] = f"{date_from}T00:00:00Z/{date_to}T23:59:59Z"

    session = _get_session()

    async def _do_request(token: str):
        return await session.post(
            _SH_CATALOG_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=search_body,
        )

    token = await get_access_token()
    async with await _do_request(token) as resp:
        # Stale-token recovery: force-refresh once on 401 and retry.
        if resp.status == 401:
            logger.info("Catalog search got 401, refreshing token and retrying")
            token = await get_access_token(force_refresh=True)
            async with await _do_request(token) as resp2:
                if resp2.status != 200:
                    body = await resp2.text()
                    logger.warning("Catalog search failed %d: %s", resp2.status, body[:200])
                    return []
                data = await resp2.json()
        elif resp.status != 200:
            body = await resp.text()
            logger.warning("Catalog search failed %d: %s", resp.status, body[:200])
            return []
        else:
            data = await resp.json()

    results = []
    for feat in data.get("features", []):
        props = feat.get("properties", {})
        results.append({
            "datetime": props.get("datetime", ""),
            "cloud_cover": props.get("eo:cloud_cover"),
        })

    # Sort by cloud cover (clearest first) to match leastCC mosaicking
    results.sort(key=lambda s: s.get("cloud_cover") or 100)
    return results


def tile_bbox_3857(z: int, x: int, y: int) -> tuple[float, float, float, float]:
    """Convert XYZ tile coordinates to EPSG:3857 bounding box.

    Returns (min_x, min_y, max_x, max_y) in Web Mercator metres.
    """
    n = 2**z
    # Full extent of Web Mercator
    origin = 20037508.342789244
    tile_size = origin * 2 / n

    min_x = -origin + x * tile_size
    max_x = min_x + tile_size
    max_y = origin - y * tile_size
    min_y = max_y - tile_size

    return (min_x, min_y, max_x, max_y)


# ---------------------------------------------------------------------------
# Collection type mapping for Process API
# ---------------------------------------------------------------------------

# Maps our collection names to Sentinel Hub Process API data types
_COLLECTION_TYPES: dict[str, str] = {
    "sentinel-2-l2a": "sentinel-2-l2a",
    "planetscope": "planetscope",
    "skysat": "skysat",
}

# ---------------------------------------------------------------------------
# Evalscripts per collection + visualization
# ---------------------------------------------------------------------------

# PlanetScope SuperDove 8-band (BYOC)
# Bands: coastal_blue, blue, green_i, green, yellow, red, rededge, nir
_PS_EVALSCRIPTS: dict[str, str] = {
    "TRUE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["red", "green", "blue", "dataMask"] }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  return [2.5 * s.red, 2.5 * s.green, 2.5 * s.blue, s.dataMask];
}""",
    "NDVI": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["red", "nir", "dataMask"] }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  let ndvi = (s.nir - s.red) / (s.nir + s.red);
  let r, g, b;
  if (ndvi < -0.2) { r = 0.6; g = 0.6; b = 0.6; }
  else if (ndvi < 0.0) { r = 0.7; g = 0.4; b = 0.2; }
  else if (ndvi < 0.15) { r = 0.9; g = 0.6; b = 0.2; }
  else if (ndvi < 0.3) { r = 1.0; g = 0.9; b = 0.2; }
  else if (ndvi < 0.5) { r = 0.5; g = 0.8; b = 0.1; }
  else if (ndvi < 0.7) { r = 0.1; g = 0.7; b = 0.1; }
  else { r = 0.0; g = 0.4; b = 0.0; }
  return [r, g, b, s.dataMask];
}""",
    "FALSE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["red", "green", "nir", "dataMask"] }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  return [2.5 * s.nir, 2.5 * s.red, 2.5 * s.green, s.dataMask];
}""",
    "NDRE": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["rededge", "nir", "dataMask"] }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  let ndre = (s.nir - s.rededge) / (s.nir + s.rededge);
  let r, g, b;
  if (ndre < -0.1) { r = 0.5; g = 0.5; b = 0.5; }
  else if (ndre < 0.0) { r = 0.8; g = 0.2; b = 0.2; }
  else if (ndre < 0.1) { r = 0.9; g = 0.5; b = 0.1; }
  else if (ndre < 0.2) { r = 1.0; g = 0.8; b = 0.2; }
  else if (ndre < 0.35) { r = 0.4; g = 0.8; b = 0.2; }
  else if (ndre < 0.5) { r = 0.1; g = 0.6; b = 0.2; }
  else { r = 0.0; g = 0.3; b = 0.1; }
  return [r, g, b, s.dataMask];
}""",
}

# Sentinel-2 L2A — SIMPLE mosaicking (most-recent cloud-free scene)
# Uses mostRecent mosaickingOrder + maxCloudCoverage filter in the API,
# so evalscripts only need to render a single pixel — much faster than ORBIT.
# Bands: B02=blue, B03=green, B04=red, B05=rededge, B08=nir
_S2_EVALSCRIPTS: dict[str, str] = {
    "TRUE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04","B03","B02","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  if (!s.dataMask) return [0,0,0,0];
  var gain = 3.5, sat = 1.2;
  var r = s.B04, g = s.B03, b = s.B02;
  var avg = (r+g+b)/3*(1-sat);
  return [
    Math.min(1, avg+gain*r*sat),
    Math.min(1, avg+gain*g*sat),
    Math.min(1, avg+gain*b*sat), 1
  ];
}""",
    "NDVI": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04","B08","SCL","dataMask"] }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  if (!s.dataMask) return [0,0,0,0];
  // Mask clouds/snow/shadow via Scene Classification Layer
  if (s.SCL==3||s.SCL==8||s.SCL==9||s.SCL==10||s.SCL==11) return [0,0,0,0];
  var ndvi = (s.B08 - s.B04) / (s.B08 + s.B04 + 1e-10);
  // Continuous red-orange-yellow-green ramp (0→1)
  // Anchors: 0.0=red, 0.3=orange, 0.5=yellow, 0.7=light-green, 1.0=dark-green
  var t = Math.max(0, Math.min(1, ndvi));
  var r, g, b;
  if (t < 0.2) {
    var f = t / 0.2;
    r = 0.78 + f * 0.17; g = 0.13 + f * 0.30; b = 0.07 + f * 0.0;
  } else if (t < 0.35) {
    var f = (t - 0.2) / 0.15;
    r = 0.95 - f * 0.05; g = 0.43 + f * 0.37; b = 0.07 + f * 0.05;
  } else if (t < 0.5) {
    var f = (t - 0.35) / 0.15;
    r = 0.90 - f * 0.42; g = 0.80 + f * 0.05; b = 0.12 - f * 0.02;
  } else if (t < 0.65) {
    var f = (t - 0.5) / 0.15;
    r = 0.48 - f * 0.28; g = 0.85 - f * 0.10; b = 0.10 - f * 0.02;
  } else if (t < 0.8) {
    var f = (t - 0.65) / 0.15;
    r = 0.20 - f * 0.12; g = 0.75 - f * 0.15; b = 0.08 - f * 0.02;
  } else {
    var f = (t - 0.8) / 0.2;
    r = 0.08 - f * 0.06; g = 0.60 - f * 0.22; b = 0.06 - f * 0.02;
  }
  return [r, g, b, 1];
}""",
    "FALSE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B03","B04","B08","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  if (!s.dataMask) return [0,0,0,0];
  var gain = 2.5;
  return [
    Math.min(1, gain*s.B08),
    Math.min(1, gain*s.B04),
    Math.min(1, gain*s.B03), 1
  ];
}""",
    "NDRE": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B05","B08","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 }
  };
}
function evaluatePixel(s) {
  if (!s.dataMask) return [0,0,0,0];
  var ndre = (s.B08 - s.B05) / (s.B08 + s.B05);
  var r, g, b;
  if (ndre < -0.1) { r = 0.5; g = 0.5; b = 0.5; }
  else if (ndre < 0.0) { r = 0.8; g = 0.2; b = 0.2; }
  else if (ndre < 0.1) { r = 0.9; g = 0.5; b = 0.1; }
  else if (ndre < 0.2) { r = 1.0; g = 0.8; b = 0.2; }
  else if (ndre < 0.35) { r = 0.4; g = 0.8; b = 0.2; }
  else if (ndre < 0.5) { r = 0.1; g = 0.6; b = 0.2; }
  else { r = 0.0; g = 0.3; b = 0.1; }
  return [r, g, b, 1];
}""",
}

# SkySat reuses same band names as PlanetScope
_SKYSAT_EVALSCRIPTS = _PS_EVALSCRIPTS

# Collection → evalscript mapping
_EVALSCRIPTS: dict[str, dict[str, str]] = {
    "sentinel-2-l2a": _S2_EVALSCRIPTS,
    "planetscope": _PS_EVALSCRIPTS,
    "skysat": _SKYSAT_EVALSCRIPTS,
}


def get_evalscript(collection: str, layer: str) -> str:
    """Get the evalscript for a given collection and visualization layer.

    Falls back to TRUE-COLOR if the requested layer is not found.
    """
    scripts = _EVALSCRIPTS.get(collection, _S2_EVALSCRIPTS)
    return scripts.get(layer, scripts.get("TRUE-COLOR", _S2_EVALSCRIPTS["TRUE-COLOR"]))


def build_process_payload(
    collection: str,
    evalscript: str,
    bbox: tuple[float, float, float, float],
    *,
    date_from: str = "",
    date_to: str = "",
    maxcc: int = 20,
    width: int = 512,
    height: int = 512,
    mosaic: str = "leastCC",
) -> dict:
    """Build a Sentinel Hub Process API request payload.

    Args:
        collection: Data collection (sentinel-2-l2a, planetscope, skysat).
        evalscript: Evalscript to execute.
        bbox: EPSG:3857 bounding box (min_x, min_y, max_x, max_y).
        date_from: Start date ISO (e.g. 2025-05-01).
        date_to: End date ISO (e.g. 2025-05-31).
        maxcc: Maximum cloud coverage percentage.
        width: Tile width in pixels.
        height: Tile height in pixels.
        mosaic: Mosaicking order — leastCC (clearest) or mostRecent (newest).
    """
    data_type = _COLLECTION_TYPES.get(collection, collection)

    data_filter: dict = {
        "mosaickingOrder": mosaic,
    }
    if date_from and date_to:
        data_filter["timeRange"] = {
            "from": f"{date_from}T00:00:00Z",
            "to": f"{date_to}T23:59:59Z",
        }
    if data_type in ("sentinel-2-l2a",):
        data_filter["maxCloudCoverage"] = maxcc

    data_entry: dict = {
        "type": data_type,
        "dataFilter": data_filter,
        "processing": {
            "upsampling": "BICUBIC",
            "downsampling": "BILINEAR",
        },
    }

    return {
        "input": {
            "bounds": {
                "bbox": list(bbox),
                "properties": {
                    "crs": "http://www.opengis.net/def/crs/EPSG/0/3857",
                },
            },
            "data": [data_entry],
        },
        "output": {
            "width": width,
            "height": height,
            "responses": [
                {
                    "identifier": "default",
                    "format": {"type": "image/png"},
                },
            ],
        },
        "evalscript": evalscript,
    }


async def fetch_tile(
    collection: str,
    layer: str,
    bbox: tuple[float, float, float, float],
    *,
    date_from: str = "",
    date_to: str = "",
    maxcc: int = 20,
    width: int = 512,
    height: int = 512,
    mosaic: str = "leastCC",
) -> Optional[bytes]:
    """Fetch a tile from Sentinel Hub Process API.

    Returns PNG bytes on success, None on error.
    """
    evalscript = get_evalscript(collection, layer)
    payload = build_process_payload(
        collection=collection,
        evalscript=evalscript,
        bbox=bbox,
        date_from=date_from,
        date_to=date_to,
        maxcc=maxcc,
        width=width,
        height=height,
        mosaic=mosaic,
    )

    session = _get_session()

    async def _do_request(token: str):
        return await session.post(
            _SH_PROCESS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            },
            json=payload,
        )

    token = await get_access_token()
    async with await _do_request(token) as resp:
        # Stale-token recovery: force-refresh once on 401 and retry.
        if resp.status == 401:
            logger.info("Process API got 401, refreshing token and retrying")
            token = await get_access_token(force_refresh=True)
            async with await _do_request(token) as resp2:
                if resp2.status != 200:
                    body = await resp2.text()
                    logger.warning(
                        "Sentinel Hub Process API error %d (after refresh): %s",
                        resp2.status,
                        body[:300],
                    )
                    return None
                return await resp2.read()
        if resp.status != 200:
            body = await resp.text()
            logger.warning(
                "Sentinel Hub Process API error %d: %s",
                resp.status,
                body[:300],
            )
            return None
        return await resp.read()
