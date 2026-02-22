"""PMTiles generation and S3 upload for vector layers."""

import asyncio
import json
import logging
import os
import tempfile
from typing import List, Optional

from boto3.s3.transfer import TransferConfig

from src.structures import get_async_db_connection
from src.symbology.llm import generate_maplibre_layers_for_layer_id
from src.upload.models import VectorProcessingResult
from src.upload.preprocessing import get_layer_bounds_and_metadata
from src.utils import get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)

one_shot_config = TransferConfig(multipart_threshold=5 * 1024**3)  # 5 GiB


async def generate_pmtiles_from_ogr_source(
    layer_id: str,
    ogr_source: str,
    feature_count: int,
    user_id: str | None = None,
    project_id: str | None = None,
    dataset_layer: str | None = None,
) -> str:
    """Generate PMTiles from any OGR-compatible source and store in S3.

    Returns the S3 key of the uploaded PMTiles file.
    """
    bucket_name = get_bucket_name()

    with tempfile.TemporaryDirectory() as temp_dir:
        # Create local output PMTiles file
        local_output_file = os.path.join(temp_dir, f"layer_{layer_id}.pmtiles")
        # Reproject to EPSG:4326 and convert to FlatGeobuf
        reprojected_file = os.path.join(temp_dir, "reprojected.fgb")

        # Build ogr2ogr command with source-specific options
        ogr_cmd = [
            "ogr2ogr",
            "-f",
            "FlatGeobuf",
            "-t_srs",
            "EPSG:4326",
            "-nlt",
            "PROMOTE_TO_MULTI",
            "-skipfailures",
            "-lco",
            "SPATIAL_INDEX=YES",
        ]

        # Add CSV-specific options for lat/long column detection
        if ogr_source.startswith("CSV:"):
            ogr_cmd.extend(
                [
                    "-oo",
                    "X_POSSIBLE_NAMES=long,longitude,lng,x",
                    "-oo",
                    "Y_POSSIBLE_NAMES=lat,latitude,y",
                    "-oo",
                    "KEEP_GEOM_COLUMNS=NO",
                ]
            )

        ogr_cmd.extend([reprojected_file, ogr_source])
        # If a specific dataset layer is requested (e.g., GeoPackage sublayer),
        # pass it as an additional source argument to ogr2ogr to select that layer.
        if dataset_layer is not None:
            ogr_cmd.append(dataset_layer)

        process = await asyncio.create_subprocess_exec(
            *ogr_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            raise Exception(
                "Failed to reproject geospatial data. Please check that the source contains valid geometry."
            )

        # Run tippecanoe to generate pmtiles
        tippecanoe_cmd = [
            "tippecanoe",
            "-o",
            local_output_file,
            "-q",  # Quiet mode - suppress progress indicators
            "-zg",  # Always try to guess maxzoom
            "--drop-densest-as-needed",
            reprojected_file,
        ]

        process = await asyncio.create_subprocess_exec(
            *tippecanoe_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        if process.returncode != 0:
            err_text = (stderr or b"").decode("utf-8", errors="ignore")
            # If tippecanoe can't guess maxzoom for single-point datasets, fall back to ogr2ogr PMTiles
            if (
                "Can't guess maxzoom (-zg) without at least two distinct feature locations"
                in err_text
            ):
                pmtiles_ogr_cmd = [
                    "ogr2ogr",
                    "-f",
                    "PMTiles",
                    local_output_file,
                    reprojected_file,
                ]
                process2 = await asyncio.create_subprocess_exec(
                    *pmtiles_ogr_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, stderr2 = await process2.communicate()
                if process2.returncode != 0:
                    raise Exception(
                        f"ogr2ogr PMTiles fallback failed: {(stderr2 or b'').decode('utf-8', errors='ignore')}"
                    )
            else:
                raise Exception(
                    f"tippecanoe command failed with exit code {process.returncode}: {err_text}"
                )

        # Upload the PMTiles file to S3 with user_id and project_id in path if available
        if user_id and project_id:
            pmtiles_key = f"pmtiles/{user_id}/{project_id}/{layer_id}.pmtiles"
        else:
            # Fallback to old path if user_id/project_id not available
            pmtiles_key = f"pmtiles/layer/{layer_id}.pmtiles"
        s3 = await get_async_s3_client()
        await s3.upload_file(
            local_output_file, bucket_name, pmtiles_key, Config=one_shot_config
        )

        # Update the database with the PMTiles key (atomic JSONB merge — no race)
        async with get_async_db_connection() as conn:
            await conn.execute(
                """
                UPDATE map_layers
                SET metadata = COALESCE(metadata, '{}'::jsonb) || $1::jsonb
                WHERE layer_id = $2
                """,
                json.dumps({"pmtiles_key": pmtiles_key}),
                layer_id,
            )

        return pmtiles_key


async def process_vector_layer_common(
    layer_id: str,
    ogr_source: str,
    layer_name: str,
    user_id: str,
    project_id: str,
    dataset_layer: str | None = None,
) -> VectorProcessingResult:
    """Unified processing pipeline for vector layers from any source.

    Extracts metadata, generates PMTiles, and creates MapLibre styles.

    Args:
        layer_id: Generated layer ID.
        ogr_source: OGR-compatible source path or URI.
        layer_name: Display name for the layer.
        user_id: User ID for ownership.
        project_id: Project ID for organization.
        dataset_layer: Optional sublayer name.

    Returns:
        :class:`VectorProcessingResult` ready for database insertion.
    """
    # Extract bounds and metadata from the source
    layer_info = await get_layer_bounds_and_metadata(
        ogr_source, "vector", dataset_layer=dataset_layer
    )

    bounds = layer_info.bounds
    geometry_type = layer_info.geometry_type
    feature_count = layer_info.feature_count
    metadata_updates = layer_info.metadata_updates

    # Add base metadata
    metadata_updates.source = "remote" if not ogr_source.startswith("/") else "upload"
    metadata_updates.layer_name = layer_name

    # Generate PMTiles for vector layers with features
    pmtiles_key: Optional[str] = None
    if feature_count and feature_count > 0:
        try:
            pmtiles_key = await generate_pmtiles_from_ogr_source(
                layer_id,
                ogr_source,
                feature_count,
                user_id,
                project_id,
                dataset_layer=dataset_layer,
            )
            metadata_updates.pmtiles_key = pmtiles_key
        except Exception as e:
            logger.warning("PMTiles generation failed for %s: %s", ogr_source, e)
            # Continue without PMTiles - not critical

    # Generate MapLibre style for vector layers
    maplibre_style: Optional[List[dict]] = None
    if geometry_type != "unknown":
        maplibre_style = generate_maplibre_layers_for_layer_id(layer_id, geometry_type)

    return VectorProcessingResult(
        layer_id=layer_id,
        bounds=bounds,
        geometry_type=geometry_type,
        feature_count=feature_count,
        metadata=metadata_updates,
        pmtiles_key=pmtiles_key,
        maplibre_style=maplibre_style,
        layer_type="vector",
    )
