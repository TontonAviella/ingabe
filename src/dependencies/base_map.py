from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
import httpx
import os


class BaseMapProvider(ABC):
    """Abstract base class for base map providers."""

    @abstractmethod
    async def get_base_style(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Return the base MapLibre GL style JSON."""
        pass

    @abstractmethod
    def get_available_styles(self) -> List[str]:
        """Return list of available basemap style names."""
        pass

    @abstractmethod
    def get_csp_policies(self) -> Dict[str, List[str]]:
        """Return CSP policies required for this base map provider.

        Returns:
            Dict mapping CSP directive names to lists of allowed sources.
            Common directives: connect-src, img-src, font-src, style-src, script-src
        """
        pass

    @abstractmethod
    def get_style_display_names(self) -> Dict[str, str]:
        """Return mapping of style names to human-readable display names."""
        pass

    @abstractmethod
    def get_default_preview_path(self) -> str:
        """Return the absolute path to the default preview image for this provider."""
        pass


class OpenStreetMapProvider(BaseMapProvider):
    """Default base map provider using OpenStreetMap tiles."""

    # Raster basemap definitions: (source_id, tiles, tileSize, attribution, maxzoom, name)
    _RASTER_BASEMAPS: Dict[str, Dict[str, Any]] = {
        "openstreetmap": {
            "name": "OpenStreetMap",
            "tiles": ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"],
            "tileSize": 256,
            "attribution": "&copy; OpenStreetMap contributors",
            "maxzoom": 19,
        },
        "esri_satellite": {
            "name": "Esri Satellite",
            "tiles": [
                "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "attribution": "&copy; Esri, Maxar, Earthstar Geographics",
            "maxzoom": 18,
        },
        "esri_topo": {
            "name": "Esri Topographic",
            "tiles": [
                "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}"
            ],
            "tileSize": 256,
            "attribution": "&copy; Esri, HERE, Garmin, OpenStreetMap contributors",
            "maxzoom": 19,
        },
        "carto_dark": {
            "name": "Dark Matter",
            "tiles": [
                "https://basemaps.cartocdn.com/dark_all/{z}/{x}/{y}@2x.png"
            ],
            "tileSize": 256,
            "attribution": "&copy; OpenStreetMap contributors &copy; CARTO",
            "maxzoom": 20,
        },
        "carto_voyager": {
            "name": "Voyager",
            "tiles": [
                "https://basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}@2x.png"
            ],
            "tileSize": 256,
            "attribution": "&copy; OpenStreetMap contributors &copy; CARTO",
            "maxzoom": 20,
        },
        "sentinel2_live": {
            "name": "Sentinel-2 Live",
            "tiles": [
                "/api/satellite/{z}/{x}/{y}.png?layer=TRUE-COLOR&collection=sentinel-2-l2a"
            ],
            "tileSize": 512,
            "attribution": "&copy; Copernicus Sentinel-2 (ESA), processed by Sentinel Hub",
            # Sentinel-2 is 10m resolution — useful up to z14 (~10m/pixel).
            # Beyond z14, MapLibre overzooms (stretches tiles) instead of
            # requesting new API calls for data that can't be sharper.
            "maxzoom": 14,
        },
        "ndvi_map": {
            "name": "NDVI Vegetation",
            "tiles": [
                "/api/satellite/{z}/{x}/{y}.png?layer=NDVI&collection=sentinel-2-l2a"
            ],
            "tileSize": 512,
            "attribution": "&copy; Copernicus Sentinel-2 NDVI (ESA), processed by Sentinel Hub",
            "maxzoom": 14,
        },
    }

    async def get_base_style(self, name: Optional[str] = None) -> Dict[str, Any]:
        """Return a MapLibre GL style for the specified basemap.

        Args:
            name: Basemap name (default: 'openstreetmap').
                  Supports raster basemaps (openstreetmap, esri_satellite,
                  esri_topo, carto_dark, carto_voyager) and the vector
                  basemap 'openfreemap'.
        """
        basemap_name = name or "esri_satellite"

        if basemap_name == "openfreemap":
            # Fetch the OpenFreeMap vector style from their API
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    response = await client.get(
                        "https://tiles.openfreemap.org/styles/liberty"
                    )
                    response.raise_for_status()
                    return response.json()
            except (httpx.TimeoutException, httpx.HTTPStatusError):
                # Fall back to OpenStreetMap raster if OpenFreeMap is down
                basemap_name = "openstreetmap"

        # Lookup raster basemap definition
        basemap_def = self._RASTER_BASEMAPS.get(basemap_name)
        if basemap_def is None:
            # Fallback to openstreetmap
            basemap_def = self._RASTER_BASEMAPS["openstreetmap"]

        source_id = basemap_name.replace("_", "-")
        return {
            "version": 8,
            "name": basemap_def["name"],
            "metadata": {
                "maplibre:logo": "https://maplibre.org/",
            },
            "glyphs": "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
            "sources": {
                source_id: {
                    "type": "raster",
                    "tiles": basemap_def["tiles"],
                    "tileSize": basemap_def["tileSize"],
                    "attribution": basemap_def["attribution"],
                    "maxzoom": basemap_def["maxzoom"],
                }
            },
            "layers": [
                {
                    "id": source_id,
                    "type": "raster",
                    "source": source_id,
                    "layout": {"visibility": "visible"},
                    "paint": {},
                }
            ],
            "center": [0, 0],
            "zoom": 2,
            "bearing": 0,
            "pitch": 0,
        }

    def get_available_styles(self) -> List[str]:
        """Return list of available basemap style names.

        esri_satellite is first so it becomes the default for new maps.
        """
        return [
            "esri_satellite",
            # sentinel2_live and ndvi_map disabled until SH credentials are renewed
            # "sentinel2_live",
            # "ndvi_map",
            "openstreetmap",
            "openfreemap",
            "esri_topo",
            "carto_dark",
            "carto_voyager",
        ]

    def get_csp_policies(self) -> Dict[str, List[str]]:
        """Return CSP policies required for all basemap tile providers."""
        return {
            "connect-src": [
                "https://tile.openstreetmap.org",
                "https://tiles.openfreemap.org",
                "https://demotiles.maplibre.org",
                "https://server.arcgisonline.com",
                "https://ibasemaps-api.arcgis.com",
                "https://basemaps.cartocdn.com",
            ],
            "img-src": [
                "https://tile.openstreetmap.org",
                "https://tiles.openfreemap.org",
                "https://demotiles.maplibre.org",
                "https://server.arcgisonline.com",
                "https://ibasemaps-api.arcgis.com",
                "https://basemaps.cartocdn.com",
            ],
            "font-src": [
                "https://demotiles.maplibre.org",
                "https://tiles.openfreemap.org",
            ],
        }

    def get_style_display_names(self) -> Dict[str, str]:
        """Return mapping of style names to human-readable display names."""
        return {
            "openstreetmap": "OpenStreetMap",
            "openfreemap": "OpenFreeMap",
            "esri_satellite": "Satellite",
            "sentinel2_live": "Sentinel-2 Live",
            "ndvi_map": "NDVI Vegetation",
            "esri_topo": "Topographic",
            "carto_dark": "Dark Matter",
            "carto_voyager": "Voyager",
        }

    def get_default_preview_path(self) -> str:
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "osm.webp")


# Default dependency - can be overridden in closed source
def get_base_map_provider() -> BaseMapProvider:
    """Default base map provider dependency."""
    return OpenStreetMapProvider()
