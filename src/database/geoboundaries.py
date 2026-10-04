"""Rwanda admin-boundary features for the boundary seed migrations.

One home for where the seed migrations (e1f2a3b4c5d6, f2a3b4c5d6e7,
b2c3d4e5f6a7) get geoBoundaries gbOpen RWA features from. Levels: ADM2
districts, ADM3 sectors, ADM4 cells, ADM5 villages (simplified geometry).
"""

import requests

_API_URL = "https://www.geoboundaries.org/api/current/gbOpen/RWA/{level}/"
# ADM5 is read from the simplified file: the full one is ~130 MB.
_ADM5_SIMPLIFIED_URL = (
    "https://github.com/wmgeolab/geoBoundaries/raw/9469f09/"
    "releaseData/gbOpen/RWA/ADM5/"
    "geoBoundaries-RWA-ADM5_simplified.geojson"
)
_DOWNLOAD_TIMEOUT_S = {"ADM2": 120, "ADM3": 180, "ADM4": 300, "ADM5": 300}


def rwanda_boundary_features(level: str) -> list[dict]:
    """Return the GeoJSON features for one Rwanda admin level.

    Raises on any failure; callers decide whether that is fatal.
    """
    if level == "ADM5":
        geojson_url = _ADM5_SIMPLIFIED_URL
    else:
        api_resp = requests.get(_API_URL.format(level=level), timeout=30)
        api_resp.raise_for_status()
        geojson_url = api_resp.json().get("gjDownloadURL")
        if not geojson_url:
            raise RuntimeError(f"No gjDownloadURL in geoBoundaries {level} response")

    geojson_resp = requests.get(geojson_url, timeout=_DOWNLOAD_TIMEOUT_S[level])
    geojson_resp.raise_for_status()
    return geojson_resp.json().get("features", [])
