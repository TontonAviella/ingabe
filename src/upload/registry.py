"""Upload handler registry — dispatches file extensions to handlers."""

from src.upload.base import BaseUploadHandler
from src.upload.handlers.csv_handler import CSVUploadHandler
from src.upload.handlers.pointcloud_handler import PointCloudUploadHandler
from src.upload.handlers.raster_handler import RasterUploadHandler
from src.upload.handlers.vector_handler import VectorUploadHandler

# Extension → handler mapping.
# Add new formats here (e.g. ".zarr": ZarrUploadHandler()).
_HANDLER_MAP: dict[str, BaseUploadHandler] = {
    # CSV
    ".csv": CSVUploadHandler(),
    # Raster
    ".tif": RasterUploadHandler(),
    ".tiff": RasterUploadHandler(),
    ".jpg": RasterUploadHandler(),
    ".jpeg": RasterUploadHandler(),
    ".png": RasterUploadHandler(),
    ".dem": RasterUploadHandler(),
    # Point cloud
    ".las": PointCloudUploadHandler(),
    ".laz": PointCloudUploadHandler(),
    # Vector (default for everything else handled by fallback)
    ".geojson": VectorUploadHandler(),
    ".fgb": VectorUploadHandler(),
    ".gpkg": VectorUploadHandler(),
    ".shp": VectorUploadHandler(),
    ".kml": VectorUploadHandler(),
    ".kmz": VectorUploadHandler(),
    ".zip": VectorUploadHandler(),
    ".pmtiles": VectorUploadHandler(),
}

# Default handler for unrecognized extensions
_DEFAULT_HANDLER = VectorUploadHandler()

RASTER_EXTS = frozenset({".tif", ".tiff", ".jpg", ".jpeg", ".png", ".dem"})
POINT_CLOUD_EXTS = frozenset({".las", ".laz"})


def get_handler(file_ext: str) -> BaseUploadHandler:
    """Return the appropriate handler for a file extension."""
    return _HANDLER_MAP.get(file_ext.lower(), _DEFAULT_HANDLER)


def get_layer_type(file_ext: str) -> str:
    """Determine layer type from file extension."""
    ext = file_ext.lower()
    if ext in RASTER_EXTS:
        return "raster"
    if ext in POINT_CLOUD_EXTS:
        return "point_cloud"
    return "vector"
