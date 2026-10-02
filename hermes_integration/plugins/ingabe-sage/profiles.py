"""Task-scoped Hermes tool profiles for Sage.

Hermes sends every enabled tool schema to the model. Keeping the complete
Ingabe catalog in one toolset made even trivial turns carry tens of thousands
of prompt tokens. Profiles keep the full capability surface installed while
only exposing the tools relevant to the current task.
"""
from __future__ import annotations

CORE_TOOLSET = "ingabe-sage-core"
MAP_VIEW_TOOLSET = "ingabe-sage-map-view"
MAP_DATA_TOOLSET = "ingabe-sage-map-data"
MAP_PROCESS_TOOLSET = "ingabe-sage-map-process"
RASTER_ENGINE_TOOLSET = "ingabe-sage-raster-engine"
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
    MAP_PROCESS_TOOLSET,
    RASTER_ENGINE_TOOLSET,
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
_RASTER_ENGINE_MARKERS = ("spatial_engine", "geolibre")
_RASTER_SENSOR_MARKERS = (
    "alos",
    "cygnss",
    "soil_moisture",
)
_RASTER_VISION_MARKERS = (
    "rgb",
    "flood",
    "water",
    "sphere",
    "raster_h3",
    "raster_object",
)
_RASTER_ANALYSIS_MARKERS = (
    "raster",
    "pixel",
    "spectral",
    "zonal",
)
_MAP_DATA_MARKERS = (
    "postgis",
    "duckdb",
    "database",
)
_MAP_VIEW_MARKERS = (
    "layer",
    "map",
    "geojson",
    "zoom",
    "display",
    "render",
    "reverse_geocode",
)
_MAP_PROCESS_MARKERS = (
    "buffer",
    "clip",
    "intersection",
    "reproject",
    "dissolve",
    "aggregate",
    "fieldcalculator",
    "fixgeometries",
    "creategrid",
    "warpreproject",
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
    "food_security",
    "emission",
    "expected_rain",
)


def toolset_for_tool(name: str) -> str:
    normalized = name.strip().lower()
    if normalized in _CORE_TOOLS:
        return CORE_TOOLSET
    if any(marker in normalized for marker in _BRAIN_MARKERS):
        return BRAIN_TOOLSET
    if any(marker in normalized for marker in _RASTER_ENGINE_MARKERS):
        return RASTER_ENGINE_TOOLSET
    if any(marker in normalized for marker in _RASTER_SENSOR_MARKERS):
        return RASTER_SENSOR_TOOLSET
    if any(marker in normalized for marker in _RASTER_VISION_MARKERS):
        return RASTER_VISION_TOOLSET
    if any(marker in normalized for marker in _RASTER_ANALYSIS_MARKERS):
        return RASTER_ANALYSIS_TOOLSET
    if any(marker in normalized for marker in _MAP_DATA_MARKERS):
        return MAP_DATA_TOOLSET
    if normalized.startswith(("native_", "qgis_", "gdal_")) or any(
        marker in normalized for marker in _MAP_PROCESS_MARKERS
    ):
        return MAP_PROCESS_TOOLSET
    if any(marker in normalized for marker in _MAP_VIEW_MARKERS):
        return MAP_VIEW_TOOLSET
    if any(marker in normalized for marker in _AGRI_WEATHER_MARKERS):
        return AGRI_WEATHER_TOOLSET
    if any(marker in normalized for marker in _AGRI_RISK_MARKERS):
        return AGRI_RISK_TOOLSET
    return AGRI_FIELD_TOOLSET
