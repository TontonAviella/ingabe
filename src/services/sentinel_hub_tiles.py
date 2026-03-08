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
import time
from typing import Optional

import aiohttp

from src.config import settings

logger = logging.getLogger(__name__)

# Sentinel Hub OAuth2 token endpoint
_SH_TOKEN_URL = (
    "https://services.sentinel-hub.com/auth/realms/main/"
    "protocol/openid-connect/token"
)

# Process API endpoint
_SH_PROCESS_URL = "https://services.sentinel-hub.com/api/v1/process"

# Cached token state
_cached_token: Optional[str] = None
_token_expires_at: float = 0.0


def is_configured() -> bool:
    """Check if Sentinel Hub tile proxy credentials are set."""
    return bool(settings.sh_client_id and settings.sh_client_secret)


async def get_access_token() -> str:
    """Return a valid OAuth2 access token, fetching a new one if expired.

    Tokens are cached in memory with a 5-minute safety margin before expiry.
    """
    global _cached_token, _token_expires_at

    if _cached_token and time.monotonic() < _token_expires_at:
        return _cached_token

    if not is_configured():
        raise RuntimeError("Sentinel Hub credentials not configured")

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _SH_TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": settings.sh_client_id,
                "client_secret": settings.sh_client_secret,
            },
            timeout=aiohttp.ClientTimeout(total=15),
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

# Sentinel-2 L2A — 30-day median composites with SCL cloud masking
# Uses ORBIT mosaicking to combine multiple cloud-free passes into clean tiles.
# SCL classes 4-7 = vegetation, bare soil, water, snow (cloud-free pixels).
# Bands: B02=blue, B03=green, B04=red, B05=rededge, B08=nir, SCL=scene class
_S2_EVALSCRIPTS: dict[str, str] = {
    "TRUE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04","B03","B02","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 },
    mosaicking: "ORBIT"
  };
}
function median(arr) {
  arr.sort(function(a,b){return a-b});
  var m = Math.floor(arr.length/2);
  return arr.length % 2 ? arr[m] : (arr[m-1]+arr[m])/2;
}
function evaluatePixel(samples) {
  var vR=[],vG=[],vB=[];
  for (var i=0; i<samples.length; i++) {
    var s = samples[i];
    if (!s.dataMask) continue;
    var bright = (s.B04+s.B03+s.B02)/3;
    if (bright > 0.3) continue;
    vR.push(s.B04); vG.push(s.B03); vB.push(s.B02);
  }
  if (!vR.length) return [0,0,0,0];
  var r=median(vR), g=median(vG), b=median(vB);
  var gain=3.5, sat=1.2;
  var avg=(r+g+b)/3*(1-sat);
  return [
    Math.min(1, avg+gain*r*sat),
    Math.min(1, avg+gain*g*sat),
    Math.min(1, avg+gain*b*sat), 1
  ];
}""",
    "NDVI": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B04","B08","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 },
    mosaicking: "ORBIT"
  };
}
function median(arr) {
  arr.sort(function(a,b){return a-b});
  var m = Math.floor(arr.length/2);
  return arr.length % 2 ? arr[m] : (arr[m-1]+arr[m])/2;
}
function evaluatePixel(samples) {
  var vR=[],vN=[];
  for (var i=0; i<samples.length; i++) {
    var s = samples[i];
    if (!s.dataMask) continue;
    if ((s.B04+s.B08)/2 > 0.4) continue;
    vR.push(s.B04); vN.push(s.B08);
  }
  if (!vR.length) return [0,0,0,0];
  var red=median(vR), nir=median(vN);
  var ndvi = (nir - red) / (nir + red);
  var r, g, b;
  if (ndvi < -0.2) { r = 0.6; g = 0.6; b = 0.6; }
  else if (ndvi < 0.0) { r = 0.7; g = 0.4; b = 0.2; }
  else if (ndvi < 0.15) { r = 0.9; g = 0.6; b = 0.2; }
  else if (ndvi < 0.3) { r = 1.0; g = 0.9; b = 0.2; }
  else if (ndvi < 0.5) { r = 0.5; g = 0.8; b = 0.1; }
  else if (ndvi < 0.7) { r = 0.1; g = 0.7; b = 0.1; }
  else { r = 0.0; g = 0.4; b = 0.0; }
  return [r, g, b, 1];
}""",
    "FALSE-COLOR": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B03","B04","B08","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 },
    mosaicking: "ORBIT"
  };
}
function median(arr) {
  arr.sort(function(a,b){return a-b});
  var m = Math.floor(arr.length/2);
  return arr.length % 2 ? arr[m] : (arr[m-1]+arr[m])/2;
}
function evaluatePixel(samples) {
  var vG=[],vR=[],vN=[];
  for (var i=0; i<samples.length; i++) {
    var s = samples[i];
    if (!s.dataMask) continue;
    if ((s.B04+s.B03)/2 > 0.3) continue;
    vG.push(s.B03); vR.push(s.B04); vN.push(s.B08);
  }
  if (!vG.length) return [0,0,0,0];
  var gain=2.5;
  return [
    Math.min(1, gain*median(vN)),
    Math.min(1, gain*median(vR)),
    Math.min(1, gain*median(vG)), 1
  ];
}""",
    "NDRE": """//VERSION=3
function setup() {
  return {
    input: [{ bands: ["B05","B08","dataMask"], units: "REFLECTANCE" }],
    output: { bands: 4 },
    mosaicking: "ORBIT"
  };
}
function median(arr) {
  arr.sort(function(a,b){return a-b});
  var m = Math.floor(arr.length/2);
  return arr.length % 2 ? arr[m] : (arr[m-1]+arr[m])/2;
}
function evaluatePixel(samples) {
  var vRE=[],vN=[];
  for (var i=0; i<samples.length; i++) {
    var s = samples[i];
    if (!s.dataMask) continue;
    if ((s.B05+s.B08)/2 > 0.4) continue;
    vRE.push(s.B05); vN.push(s.B08);
  }
  if (!vRE.length) return [0,0,0,0];
  var re=median(vRE), nir=median(vN);
  var ndre = (nir - re) / (nir + re);
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
    """
    data_type = _COLLECTION_TYPES.get(collection, collection)

    data_filter: dict = {
        "mosaickingOrder": "mostRecent",
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
) -> Optional[bytes]:
    """Fetch a tile from Sentinel Hub Process API.

    Returns PNG bytes on success, None on error.
    """
    token = await get_access_token()
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
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            _SH_PROCESS_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "image/png",
            },
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning(
                    "Sentinel Hub Process API error %d: %s",
                    resp.status,
                    body[:300],
                )
                return None
            return await resp.read()
