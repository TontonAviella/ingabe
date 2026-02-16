"""CSV upload handler — detects lat/lon columns and converts to FlatGeobuf."""

import asyncio
import csv
import logging
import subprocess
from io import StringIO

from fastapi import HTTPException, status

from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext
from src.upload.handlers.vector_handler import VectorUploadHandler

logger = logging.getLogger(__name__)


class CSVUploadHandler(BaseUploadHandler):
    """Handles geocoded CSV uploads.

    Preprocessing:
    1. Read CSV header to detect X/Y column names.
    2. Run ``ogr2ogr`` to convert CSV → FlatGeobuf with spatial index.
    3. Delegate to :class:`VectorUploadHandler` for layer creation.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        auxiliary_path = ctx.temp_file_path + ".fgb"

        # Detect column names for X/Y in a case-insensitive way from the header
        with open(ctx.temp_file_path, "r", encoding="utf-8-sig", errors="replace") as f:
            sample_text = f.readline()
        reader = csv.reader(StringIO(sample_text))

        normalized = {h.strip().lower(): h for h in next(reader, [])}
        detected_x = next(
            (
                normalized[col]
                for col in ["lon", "long", "longitude", "lng", "x"]
                if col in normalized
            ),
            None,
        )
        detected_y = next(
            (
                normalized[col]
                for col in ["lat", "latitude", "y"]
                if col in normalized
            ),
            None,
        )

        if not detected_x or not detected_y:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "CSV header must include longitude and latitude columns. "
                    "Accepted names (case-insensitive): "
                    "X: lon, long, longitude, lng, x; "
                    "Y: lat, latitude, y."
                ),
            )

        ogr_cmd = [
            "ogr2ogr",
            "-if", "CSV",
            "-f", "FlatGeobuf",
            auxiliary_path,
            ctx.temp_file_path,
            "-oo", f"X_POSSIBLE_NAMES={detected_x}",
            "-oo", f"Y_POSSIBLE_NAMES={detected_y}",
            "-lco", "SPATIAL_INDEX=YES",
            "-a_srs", "EPSG:4326",
        ]
        try:
            process = await asyncio.create_subprocess_exec(
                *ogr_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await process.communicate()

            if process.returncode != 0:
                raise subprocess.CalledProcessError(
                    process.returncode, ogr_cmd, stderr=stderr.decode()
                )
        except subprocess.CalledProcessError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to convert CSV to spatial format, make sure CSV has a column named lat/lon/long/lng, latitude/longitude, or x/y.",
            )

        ctx.metadata_dict["original_format"] = "csv"

        return HandlerResult(
            layer_type="vector",
            updated_temp_file_path=auxiliary_path,
            updated_file_ext=".fgb",
            updated_s3_key=f"uploads/{ctx.user_id}/{ctx.project_id}/{ctx.layer_id}.fgb",
        )

    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Delegate to VectorUploadHandler for layer creation."""
        vector_handler = VectorUploadHandler()
        return await vector_handler.create_layers(ctx, result)
