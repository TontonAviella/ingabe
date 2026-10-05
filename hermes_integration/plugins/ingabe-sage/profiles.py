"""Task-scoped Hermes tool profiles for Sage.

Hermes sends every enabled tool schema to the model. Keeping the complete
Ingabe catalog in one toolset made even trivial turns carry tens of thousands
of prompt tokens. Profiles keep the full capability surface installed while
only exposing the tools relevant to the current task.
"""
from __future__ import annotations

import re

CORE_TOOLSET = "ingabe-sage-core"
MAP_VIEW_TOOLSET = "ingabe-sage-map-view"
MAP_DATA_TOOLSET = "ingabe-sage-map-data"
RASTER_VISION_TOOLSET = "ingabe-sage-raster-vision"
RASTER_ANALYSIS_TOOLSET = "ingabe-sage-raster-analysis"
RASTER_SENSOR_TOOLSET = "ingabe-sage-raster-sensor"
AGRI_FIELD_TOOLSET = "ingabe-sage-agri-field"
AGRI_WEATHER_TOOLSET = "ingabe-sage-agri-weather"
AGRI_RISK_TOOLSET = "ingabe-sage-agri-risk"
BRAIN_TOOLSET = "ingabe-sage-brain"

ALL_TOOLSETS = (
    CORE_TOOLSET,
    MAP_VIEW_TOOLSET,
    MAP_DATA_TOOLSET,
    RASTER_VISION_TOOLSET,
    RASTER_ANALYSIS_TOOLSET,
    RASTER_SENSOR_TOOLSET,
    AGRI_FIELD_TOOLSET,
    AGRI_WEATHER_TOOLSET,
    AGRI_RISK_TOOLSET,
    BRAIN_TOOLSET,
)

_CORE_TOOLS = {
    "ingabe_whoami",
    "search_location",
}

_BRAIN_MARKERS = ("brain", "entity", "observation", "trajectory")
_RASTER_SENSOR_MARKERS = ("soil_moisture",)
_RASTER_VISION_MARKERS = (
    "rgb",
    "flood",
    "raster_h3",
    "raster_object",
)
_RASTER_ANALYSIS_MARKERS = (
    "raster",
    "pixel",
    "spectral",
    "zonal",
    "value_distribution",
)
_MAP_DATA_MARKERS = ("postgis",)
_MAP_VIEW_MARKERS = (
    "layer",
    "map",
    "geojson",
    "zoom",
    "display",
    "render",
    "reverse_geocode",
    "admin",
)
_AGRI_WEATHER_MARKERS = (
    "weather",
    "forecast",
    "dry_spell",
    "evapotranspiration",
)
_AGRI_RISK_MARKERS = (
    "risk",
    "anomaly",
    "drought",
    "insurance",
)


# Words in a user request that open a profile. Each profile's tool-name
# markers above are added automatically (split on "_"), so a request that
# names a tool always opens the profile that tool lives in.
_EXTRA_SELECTION_WORDS = {
    MAP_VIEW_TOOLSET: {
        "show", "style", "boundary", "location", "district", "sector", "cell", "village",
    },
    MAP_DATA_TOOLSET: {"sql", "query", "table"},
    RASTER_VISION_TOOLSET: {
        "raster", "orthophoto", "drone", "pixel", "image", "imagery", "building",
        "roof", "road", "tree", "object", "mask", "segment", "fastsam", "house",
    },
    RASTER_ANALYSIS_TOOLSET: {
        "geotiff", "cog", "dem", "terrain", "hydrology", "lidar", "statistic",
        "compare", "health", "value", "distribution",
    },
    RASTER_SENSOR_TOOLSET: {"sar", "radar", "soil", "moisture"},
    AGRI_FIELD_TOOLSET: {
        "crop", "field", "farm", "soil", "ndvi", "vegetation", "satellite",
        "sentinel", "landsat", "agriculture", "parcel", "management", "zone",
        "stress", "worldcover", "imagery", "agri", "index", "indices",
    },
    AGRI_WEATHER_TOOLSET: {"rain", "rainfall", "temperature", "dry", "spell"},
    AGRI_RISK_TOOLSET: {"yield", "stress", "trigger", "exposure", "payout"},
    BRAIN_TOOLSET: {"remember", "previous", "history"},
}

_MARKERS_BY_TOOLSET = {
    BRAIN_TOOLSET: _BRAIN_MARKERS,
    RASTER_SENSOR_TOOLSET: _RASTER_SENSOR_MARKERS,
    RASTER_VISION_TOOLSET: _RASTER_VISION_MARKERS,
    RASTER_ANALYSIS_TOOLSET: _RASTER_ANALYSIS_MARKERS,
    MAP_DATA_TOOLSET: _MAP_DATA_MARKERS,
    MAP_VIEW_TOOLSET: _MAP_VIEW_MARKERS,
    AGRI_WEATHER_TOOLSET: _AGRI_WEATHER_MARKERS,
    AGRI_RISK_TOOLSET: _AGRI_RISK_MARKERS,
}

SELECTION_WORDS: dict[str, frozenset[str]] = {
    toolset: frozenset(
        words
        | {
            part
            for marker in _MARKERS_BY_TOOLSET.get(toolset, ())
            for part in marker.split("_")
            if len(part) > 2
        }
    )
    for toolset, words in _EXTRA_SELECTION_WORDS.items()
}

# A domain-neutral request this long still gets map tools; shorter ones
# (small talk, "who am I") stay on the core profile.
NEUTRAL_REQUEST_MIN_TOKENS = 8

# Shorter markers ("map", "rgb") would match inside unrelated words.
_SUBSTRING_MARKER_MIN_LEN = 5


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def request_tokens(text: str) -> set[str]:
    """Lower-case word tokens plus naive singulars ("fields" -> "field")."""
    tokens = _words(text)
    singulars = set()
    for token in tokens:
        if len(token) > 4 and token.endswith("ies"):
            singulars.add(token[:-3] + "y")
        elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            singulars.add(token[:-1])
    return tokens | singulars


def select_profiles(user_text: str) -> set[str]:
    """Profiles a request should expose; always includes the core profile."""
    tokens = request_tokens(user_text)
    selected = {CORE_TOOLSET}
    for toolset, words in SELECTION_WORDS.items():
        if tokens & words:
            selected.add(toolset)
    # Requests can run words together ("dryspell"), so also match each
    # profile's longer single-word markers inside a token, the same
    # substring rule toolset_for_tool uses.
    for toolset, markers in _MARKERS_BY_TOOLSET.items():
        if any(
            marker in token
            for marker in markers
            if "_" not in marker and len(marker) >= _SUBSTRING_MARKER_MIN_LEN
            for token in tokens
        ):
            selected.add(toolset)
    if len(selected) == 1 and len(_words(user_text)) >= NEUTRAL_REQUEST_MIN_TOKENS:
        selected.add(MAP_VIEW_TOOLSET)
    return selected


def toolset_for_tool(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in _CORE_TOOLS:
        return CORE_TOOLSET
    if any(marker in normalized for marker in _BRAIN_MARKERS):
        return BRAIN_TOOLSET
    if any(marker in normalized for marker in _RASTER_SENSOR_MARKERS):
        return RASTER_SENSOR_TOOLSET
    if any(marker in normalized for marker in _RASTER_VISION_MARKERS):
        return RASTER_VISION_TOOLSET
    if any(marker in normalized for marker in _RASTER_ANALYSIS_MARKERS):
        return RASTER_ANALYSIS_TOOLSET
    if any(marker in normalized for marker in _MAP_DATA_MARKERS):
        return MAP_DATA_TOOLSET
    if any(marker in normalized for marker in _MAP_VIEW_MARKERS):
        return MAP_VIEW_TOOLSET
    if any(marker in normalized for marker in _AGRI_WEATHER_MARKERS):
        return AGRI_WEATHER_TOOLSET
    if any(marker in normalized for marker in _AGRI_RISK_MARKERS):
        return AGRI_RISK_TOOLSET
    return AGRI_FIELD_TOOLSET
