"""Upload handler registry — dispatches file extensions to handlers."""

import asyncio
import zipfile

from src.services import photo_inspections
from src.upload.archive import member_names
from src.upload.base import BaseUploadHandler
from src.upload.handlers.csv_handler import CSVUploadHandler
from src.upload.handlers.photo_set_handler import PhotoSetUploadHandler
from src.upload.handlers.pointcloud_handler import PointCloudUploadHandler
from src.upload.handlers.raster_handler import RasterUploadHandler, gdalinfo_json, has_map_position
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

# Photo inspections are recognised by what the file holds, not its extension (see handler_for).
_PHOTO_SET_HANDLER = PhotoSetUploadHandler()

RASTER_EXTS = frozenset({".tif", ".tiff", ".jpg", ".jpeg", ".png", ".dem"})
POINT_CLOUD_EXTS = frozenset({".las", ".laz"})


def get_handler(file_ext: str) -> BaseUploadHandler:
    """Return the appropriate handler for a file extension."""
    return _HANDLER_MAP.get(file_ext.lower(), _DEFAULT_HANDLER)


async def handler_for(file_ext: str, file_path: str) -> BaseUploadHandler:
    """The handler for the uploaded file at `file_path`: get_handler(file_ext), except that with photo inspections
    switched on a ZIP of photos (no shapefile in it) and a JPG/PNG that GDAL cannot place on the map become a photo
    inspection."""
    ext = file_ext.lower()
    if photo_inspections.enabled():
        if ext == ".zip" and await asyncio.to_thread(_holds_photo_set, file_path):
            return _PHOTO_SET_HANDLER
        if ext in photo_inspections.PHOTO_EXTENSIONS:
            info = await gdalinfo_json(file_path)
            if info is None or not has_map_position(info):
                return _PHOTO_SET_HANDLER
    return get_handler(ext)


def _holds_photo_set(zip_path: str) -> bool:
    try:
        return photo_inspections.is_photo_set(member_names(zip_path))
    except zipfile.BadZipFile:
        return False  # not a ZIP: the vector path reports it as today


def get_layer_type(file_ext: str) -> str:
    """Determine layer type from file extension."""
    ext = file_ext.lower()
    if ext in RASTER_EXTS:
        return "raster"
    if ext in POINT_CLOUD_EXTS:
        return "point_cloud"
    return "vector"
