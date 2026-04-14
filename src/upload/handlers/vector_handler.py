"""Vector upload handler — GeoJSON, FlatGeobuf, GeoPackage, KML, Shapefile (ZIP)."""

import json
import logging
import os
import shutil
import uuid

import fiona
from fastapi import HTTPException, status

from src.symbology.llm import generate_maplibre_layers_for_layer_id
from src.upload.base import BaseUploadHandler, HandlerResult, UploadContext
from src.upload.pmtiles import process_vector_layer_common
from src.utils import process_kmz_to_kml, process_zip_with_shapefile, generate_id as _generate_id

logger = logging.getLogger(__name__)

# Dagster integration (Phase 3): Set USE_DAGSTER=true to delegate processing
# to Dagster pipelines for FlatGeoBuf conversion, PMTiles generation, and
# Iceberg registration. Uploads complete faster with async processing.
_USE_DAGSTER = os.environ.get("USE_DAGSTER", "false").lower() in ("true", "1", "yes")


class VectorUploadHandler(BaseUploadHandler):
    """Handles vector file uploads (GeoJSON, FGB, GPKG, KML, KMZ, ZIP).

    Preprocessing handles KMZ extraction and ZIP/Shapefile → GPKG conversion.
    Layer creation iterates sublayers, generates PMTiles and MapLibre styles.
    """

    async def preprocess(self, ctx: UploadContext) -> HandlerResult:
        result = HandlerResult(layer_type="vector")

        # KML / KMZ handling
        if ctx.file_ext in (".kml", ".kmz"):
            if ctx.file_ext == ".kmz":
                temp_dir = None
                try:
                    kml_file_path, temp_dir = process_kmz_to_kml(ctx.temp_file_path)
                    result.updated_temp_file_path = kml_file_path
                    result.updated_file_ext = ".kml"
                    result.temp_dir_to_cleanup = temp_dir
                except ValueError:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="KMZ file does not contain any KML files",
                    )
                except Exception as e:
                    logger.error("Error processing KMZ file: %s", e)
                    if temp_dir:
                        shutil.rmtree(temp_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Error processing KMZ file",
                    )
            # KML: no conversion needed, process in-place

        # ZIP / Shapefile handling
        elif ctx.file_ext == ".zip":
            temp_dir = None
            try:
                gpkg_file_path, temp_dir = await process_zip_with_shapefile(
                    ctx.temp_file_path
                )
                result.updated_temp_file_path = gpkg_file_path
                result.updated_file_ext = ".gpkg"
                result.updated_s3_key = (
                    f"uploads/{ctx.map_id}/{uuid.uuid4()}.gpkg"
                )
                result.temp_dir_to_cleanup = temp_dir
                ctx.metadata_dict.update(
                    {
                        "original_format": "shapefile_zip",
                        "converted_to": "gpkg",
                    }
                )
            except ValueError as e:
                logger.warning("Error processing ZIP file: %s", e)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"ZIP file does not contain any shapefiles: {e}",
                )
            except Exception as e:
                logger.error("Error processing ZIP file: %s", e)
                if temp_dir:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Error processing ZIP file: {e}",
                )

        return result

    async def create_layers(
        self, ctx: UploadContext, result: HandlerResult
    ) -> HandlerResult:
        """Iterate sublayers, run vector processing pipeline, insert DB rows.

        When USE_DAGSTER is enabled, Dagster pipelines will handle additional
        processing (FlatGeoBuf conversion, PMTiles optimization, Iceberg registration)
        after the initial upload completes.
        """
        if _USE_DAGSTER:
            logger.info("USE_DAGSTER enabled - Additional vector processing will be handled by Dagster pipelines for map %s", ctx.map_id)

        temp_file_path = result.updated_temp_file_path or ctx.temp_file_path

        try:
            sublayers = fiona.listlayers(temp_file_path)
        except Exception:
            logger.debug("fiona.listlayers failed for %s, treating as single layer", temp_file_path)
            sublayers = []
        multi = len(sublayers) > 1
        if not sublayers:
            sublayers = [None]

        for idx, sub in enumerate(sublayers):
            this_layer_id = ctx.layer_id if idx == 0 else _generate_id(prefix="L")
            if ctx.layer_name:
                display_name = (
                    f"{ctx.layer_name} - {sub}" if (multi and sub) else ctx.layer_name
                )
            else:
                display_name = str(sub) if (multi and sub) else ctx.file_basename

            lr = await process_vector_layer_common(
                this_layer_id,
                temp_file_path,
                display_name,
                ctx.user_id,
                ctx.project_id,
                dataset_layer=sub if isinstance(sub, str) else None,
            )

            # Skip empty sublayers entirely
            if lr.feature_count is None or lr.feature_count == 0:
                continue

            per_md = {
                **ctx.metadata_dict,
                **lr.metadata.model_dump(exclude_none=True),
            }

            await ctx.conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count, s3_key, size_bytes, source_map_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                """,
                this_layer_id,
                ctx.user_id,
                display_name,
                "vector",
                json.dumps(per_md),
                lr.bounds,
                lr.geometry_type,
                lr.feature_count,
                result.updated_s3_key or ctx.s3_key,
                ctx.file_size_bytes,
                ctx.map_id,
            )

            if lr.geometry_type and lr.geometry_type != "unknown":
                ml_layers = generate_maplibre_layers_for_layer_id(
                    this_layer_id, lr.geometry_type
                )
                style_id = _generate_id(prefix="S")
                await ctx.conn.execute(
                    """
                    INSERT INTO layer_styles
                    (style_id, layer_id, style_json, created_by)
                    VALUES ($1, $2, $3, $4)
                    """,
                    style_id,
                    this_layer_id,
                    json.dumps(ml_layers),
                    ctx.user_id,
                )
                await ctx.conn.execute(
                    """
                    INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                    VALUES ($1, $2, $3)
                    """,
                    ctx.map_id,
                    this_layer_id,
                    style_id,
                )

            result.created_layer_ids.append(this_layer_id)
            if result.first_layer_url is None:
                result.first_layer_url = f"/api/layer/{this_layer_id}.pmtiles"
                result.first_layer_name = display_name

        # Enqueue brain hook: auto-create brain pages from vector features
        if result.created_layer_ids:
            try:
                from src.dependencies.brain_dep import get_brain_service
                brain_svc = get_brain_service()
                await brain_svc.enqueue_hook(ctx.conn, "vector_upload", {
                    "layer_ids": result.created_layer_ids,
                    "layer_name": ctx.layer_name or ctx.file_basename,
                    "user_id": ctx.user_id,
                    "bounds": result.bounds,
                })
            except Exception:
                logger.debug("Brain hook enqueue skipped (tables may not exist yet)")

        return result
