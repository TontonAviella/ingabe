from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.raster_object_candidates import (
    RasterObjectCandidateInput,
    analyze_raster_object_candidates as analyze_raster_object_candidates_service,
)
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class AnalyzeRasterObjectCandidatesArgs(BaseModel):
    layer_id: str = Field(
        ...,
        description="The layer_id of the uploaded drone/orthophoto raster to segment for object candidates.",
    )
    target_classes: list[str] = Field(
        ...,
        description=(
            "Objects to screen for, e.g. ['building'], ['road'], ['building','road'], "
            "['linear_boundary'], ['vegetation_patch'], ['water'], or ['bare_rectangle']."
        ),
    )
    max_candidates: int = Field(
        ...,
        description="Maximum candidate polygons to return/render. Use 100-500 for live map responses.",
    )
    max_sample_pixels: int = Field(
        ...,
        description=(
            "Maximum pixels sampled from the raster before segmentation. Use 300000-1200000 live; "
            "higher detects smaller objects but is slower."
        ),
    )
    min_area_m2: float = Field(
        ...,
        description="Minimum candidate area in square meters. For houses use 8-15.",
    )
    max_area_m2: float = Field(
        ...,
        description="Maximum candidate area in square meters. For houses use 800-1500.",
    )
    confidence_threshold: float = Field(
        ...,
        description="Minimum candidate confidence from 0 to 1. Use 0.35 for broad recall, 0.55+ for stricter screening.",
    )
    engine_preference: str = Field(
        ...,
        description="Engine preference: auto, samgeo, yolo, geolibre_rust, or rasterio_numpy. Falls back honestly if unavailable.",
    )
    render_map: bool = Field(
        ...,
        description="Whether to immediately render candidate polygons on the map. Usually true.",
    )


async def analyze_raster_object_candidates(
    args: AnalyzeRasterObjectCandidatesArgs,
    meta: IngabeToolCallMetaArgs,
) -> dict[str, Any]:
    """Find and render object candidate polygons from an uploaded orthophoto.

    Use this for questions like "where are the houses?", "how many likely
    houses are in this orthophoto?", "show roads/trees/playing areas", or
    "detect visible objects in this drone raster." This is the raster object
    path that should run before H3 risk cells when the user asks for objects or
    counts. Counts are candidate counts unless verified by Open Buildings, OSM,
    local survey, SAM/YOLO, or human review.
    """

    from src.structures import get_async_read_connection
    from src.utils import get_async_s3_client, get_bucket_name

    async with get_async_read_connection() as conn:
        row = await conn.fetchrow(
            """
            SELECT layer_id, name, type, s3_key, bounds, metadata, owner_uuid
            FROM map_layers
            WHERE layer_id = $1
            """,
            args.layer_id,
        )
    if not row:
        return {"error": f"Layer {args.layer_id} not found."}
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        return {"error": f"Layer {args.layer_id} is not owned by you."}
    if row["type"] != "raster":
        return {"error": f"Layer {args.layer_id} is type '{row['type']}', not a raster."}

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    source_key = metadata.get("cog_key") or row["s3_key"]
    source_storage = "cog" if metadata.get("cog_key") else "raw_tiff"
    if not source_key:
        return {"error": f"Layer {args.layer_id} has no readable raster object key."}

    s3_client = await get_async_s3_client()
    raster_url = await s3_client.generate_presigned_url(
        "get_object",
        Params={"Bucket": get_bucket_name(), "Key": source_key},
        ExpiresIn=900,
    )

    if os.environ.get("MUNDI_DEV_ALLOW_UNSAFE_SSL") == "1":
        os.environ.setdefault("GDAL_HTTP_UNSAFESSL", "YES")

    try:
        result = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: analyze_raster_object_candidates_service(
                    RasterObjectCandidateInput(
                        raster_url=raster_url,
                        layer_id=args.layer_id,
                        layer_name=row["name"],
                        bounds_wgs84=list(row["bounds"]) if row["bounds"] else None,
                        target_classes=args.target_classes,
                        max_candidates=args.max_candidates,
                        max_sample_pixels=args.max_sample_pixels,
                        min_area_m2=args.min_area_m2,
                        max_area_m2=args.max_area_m2,
                        confidence_threshold=args.confidence_threshold,
                        engine_preference=args.engine_preference,
                    )
                ),
            ),
            timeout=120,
        )
    except Exception as exc:
        logger.exception("raster object candidate extraction failed for layer %s", args.layer_id)
        return {"status": "error", "error": f"Raster object candidate extraction failed: {exc}"}

    if result.get("status") == "success":
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        summary["source_storage"] = source_storage
        if source_storage == "raw_tiff":
            summary["performance_note"] = (
                "This ran against the raw TIFF. Large raw drone rasters can be slow; "
                "COG/preview-backed segmentation is the fast production path."
            )
        await _upload_geoparquet_artifact(
            result,
            meta=meta,
            source_layer_id=args.layer_id,
            s3_client=s3_client,
        )

    if args.render_map and result.get("status") == "success":
        source_id = f"sage-raster-objects-{uuid.uuid4().hex[:8]}"
        style = _candidate_style()
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Rendering object candidates: {row['name']}",
            bounds=result.get("bbox"),
        ) as payload:
            payload.updates["add_geojson_layer"] = {
                "source_id": source_id,
                "geojson": result["geojson"],
                "name": f"Object Candidates - {row['name']}",
                "bounds": result.get("bbox"),
                "style_hint": "raster_object_candidates",
                "style": style,
            }
            await asyncio.sleep(0.2)
        result.setdefault("engines", {}).setdefault("render", {})["source_id"] = source_id
        result["engines"]["render"]["rendered"] = True
        result["engines"]["render"]["style_hint"] = "raster_object_candidates"

    if result.get("status") == "success":
        geojson = result["geojson"]
        result["geojson_feature_count"] = len(geojson.get("features", []))
        result["geojson"] = json.dumps(geojson)

    return result


def _candidate_style() -> dict[str, Any]:
    return {
        "color_property": "confidence",
        "stops": [
            {"max": 0.40, "color": "#facc15"},
            {"max": 0.60, "color": "#fb923c"},
            {"max": 0.80, "color": "#f97316"},
            {"max": 1.01, "color": "#ef4444"},
        ],
        "fill_opacity": 0.46,
        "stroke_color": "#fef08a",
        "stroke_width": 3,
    }


async def _upload_geoparquet_artifact(
    result: dict[str, Any],
    *,
    meta: IngabeToolCallMetaArgs,
    source_layer_id: str,
    s3_client: Any,
) -> None:
    artifact = result.get("geoparquet")
    if not isinstance(artifact, dict):
        return
    local_path = artifact.get("path")
    if not isinstance(local_path, str) or not os.path.exists(local_path):
        return

    from src.utils import get_bucket_name

    key = (
        f"geoparquet/{meta.user_uuid}/{meta.project_id}/"
        f"raster-object-candidates/{source_layer_id}-{uuid.uuid4().hex[:8]}.parquet"
    )
    try:
        await s3_client.upload_file(local_path, get_bucket_name(), key)
    except Exception as exc:
        logger.warning("Failed to upload raster object GeoParquet artifact: %s", exc)
        artifact["upload_error"] = f"{type(exc).__name__}: {exc}"
        return
    finally:
        try:
            os.remove(local_path)
        except OSError:
            pass

    artifact.pop("path", None)
    artifact["key"] = key
    artifact["storage"] = "s3"
    result["geoparquet_key"] = key
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else None
    if summary is not None:
        summary["geoparquet_key"] = key
        summary["analytics_format"] = "geoparquet"
        summary["geojson_role"] = "live_map_transport_only"
