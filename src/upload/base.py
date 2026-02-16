"""Base upload handler interface and shared data structures.

The Strategy Pattern allows each file format to define its own preprocessing,
S3 key generation, and database insertion logic. This is the seam where
Dask-based processing will plug in for raster files.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

import asyncpg


@dataclass
class UploadContext:
    """Shared context passed to every handler."""

    map_id: str
    layer_id: str
    layer_name: str
    file_basename: str
    user_id: str
    project_id: str
    temp_file_path: str
    file_ext: str
    file_size_bytes: int
    s3_key: str
    metadata_dict: dict
    conn: asyncpg.Connection
    bucket_name: str


@dataclass
class HandlerResult:
    """Result returned by each handler after processing.

    Contains everything ``internal_upload_layer`` needs to build the
    response and update the map's layer list.
    """

    created_layer_ids: list[str] = field(default_factory=list)
    first_layer_url: Optional[str] = None
    first_layer_name: Optional[str] = None
    layer_type: str = "vector"
    bounds: Optional[List[float]] = None
    # Handlers may mutate these on the UploadContext, but can also
    # override the final temp_file_path / s3_key / file_ext if they
    # convert the file to a different format.
    updated_temp_file_path: Optional[str] = None
    updated_s3_key: Optional[str] = None
    updated_file_ext: Optional[str] = None
    # Temporary directory to clean up after upload (e.g. point cloud, zip)
    temp_dir_to_cleanup: Optional[str] = None


class BaseUploadHandler(ABC):
    """Abstract base class for format-specific upload handlers.

    Subclasses implement two phases:

    1. ``preprocess`` — Convert/validate the file, mutate metadata, return
       updated paths. Called BEFORE the S3 upload.
    2. ``create_layers`` — Insert rows into ``map_layers`` / ``layer_styles``
       / ``map_layer_styles``. Called AFTER the S3 upload.
    """

    @abstractmethod
    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        """Format-specific preprocessing (conversion, metadata extraction).

        Must populate ``HandlerResult`` with any path/key/ext overrides and
        ``temp_dir_to_cleanup`` if temporary directories were created.
        """
        ...

    @abstractmethod
    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Insert layer rows into the database and populate
        ``created_layer_ids``, ``first_layer_url``, ``first_layer_name``.
        """
        ...
