from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, Field

from src.routes.websocket import kue_ephemeral_action
from src.services.raster_object_layer_persistence import (
    persist_raster_object_candidate_layer,
)
from src.services.raster_object_candidates import (
    RasterObjectCandidateInput,
    analyze_raster_object_candidates as analyze_raster_object_candidates_service,
)
from src.tools.geojson_transport import geojson_layer_update
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
        description=(
            "Engine preference: terramind_geolibre, terramind_samgeo, auto, "
            "samgeo, yolo, geolibre_rust, or rasterio_numpy. Falls back honestly "
            "if unavailable."
        ),
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
    counts. In user-facing replies, describe the result as review marks from
    the image. If the live response is capped, say the number is marks shown,
    not the number of objects. Avoid backend/data-source names unless the user
    asks how it works.
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

    payload = RasterObjectCandidateInput(
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
    timeout_seconds = _timeout_seconds_for_engine(args.engine_preference)

    try:
        result = await _run_service_with_timeout(payload, timeout_seconds)
    except asyncio.TimeoutError:
        logger.warning(
            "raster object candidate extraction timed out for layer %s using %s after %.1fs",
            args.layer_id,
            args.engine_preference,
            timeout_seconds,
        )
        if _should_fallback_after_timeout(args.engine_preference):
            fallback_payload = replace(payload, engine_preference="rasterio_numpy")
            fallback_timeout = _timeout_seconds_for_engine("rasterio_numpy")
            try:
                result = await _run_service_with_timeout(
                    fallback_payload, fallback_timeout
                )
            except asyncio.TimeoutError:
                logger.exception(
                    "raster object fallback extraction timed out for layer %s",
                    args.layer_id,
                )
                return {
                    "status": "error",
                    "error": (
                        f"The deep image pass timed out after {timeout_seconds:.0f}s, "
                        f"and the quick raster marker timed out after {fallback_timeout:.0f}s."
                    ),
                }
            except Exception as fallback_exc:
                logger.exception(
                    "raster object fallback extraction failed for layer %s",
                    args.layer_id,
                )
                return {
                    "status": "error",
                    "error": (
                        f"The deep image pass timed out after {timeout_seconds:.0f}s, "
                        f"and the quick raster marker failed: {_exception_message(fallback_exc)}"
                    ),
                }
            _annotate_timeout_fallback(
                result,
                requested_engine=args.engine_preference,
                timeout_seconds=timeout_seconds,
            )
        else:
            return {
                "status": "error",
                "error": (
                    "Raster object candidate extraction timed out after "
                    f"{timeout_seconds:.0f}s."
                ),
            }
    except Exception as exc:
        logger.exception("raster object candidate extraction failed for layer %s", args.layer_id)
        return {
            "status": "error",
            "error": (
                "Raster object candidate extraction failed: "
                f"{_exception_message(exc)}"
            ),
        }

    persisted_layer = None
    if result.get("status") == "success":
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        summary["source_storage"] = source_storage
        if source_storage == "raw_tiff":
            summary["performance_note"] = (
                "This ran against the raw TIFF. Large raw drone rasters can be slow; "
                "COG/preview-backed segmentation is the fast production path."
            )

    visible_layer_name = _visible_review_layer_name(row["name"], args.target_classes)

    if args.render_map and result.get("status") == "success":
        engines = result.setdefault("engines", {})
        render_engine = engines.setdefault("render", {})
        transport = engines.setdefault("transport", {})
        try:
            persisted_layer = await persist_raster_object_candidate_layer(
                result=result,
                user_uuid=meta.user_uuid,
                map_id=meta.map_id,
                project_id=meta.project_id,
                layer_name=visible_layer_name,
            )
        except Exception as exc:
            logger.warning(
                "Raster object layer persistence failed; using inline fallback: %s",
                exc,
                exc_info=True,
            )

        if persisted_layer:
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Saving review marks layer: {row['name']}",
                layer_id=persisted_layer.layer_id,
                update_style_json=True,
                bounds=persisted_layer.bounds or result.get("bbox"),
            ) as payload:
                payload.updates["raster_object_layer_persisted"] = {
                    "layer_id": persisted_layer.layer_id,
                    "name": visible_layer_name,
                    "pmtiles": True,
                    "geoparquet": bool(persisted_layer.geoparquet_key),
                    "pmtiles_maxzoom": persisted_layer.pmtiles_maxzoom,
                    "feature_count": persisted_layer.feature_count,
                }
                await asyncio.sleep(0.2)
            summary = (
                result.get("summary") if isinstance(result.get("summary"), dict) else {}
            )
            summary["render_transport"] = "pmtiles"
            summary["browser_transport"] = "pmtiles"
            summary["geojson_role"] = "temporary_backend_conversion_only"
            summary["geoparquet_key"] = persisted_layer.geoparquet_key
            render_engine["layer_id"] = persisted_layer.layer_id
            render_engine["rendered"] = True
            render_engine["style_hint"] = "raster_object_candidates"
            transport["current"] = "pmtiles_vector_layer"
            transport["browser"] = "PMTiles/MVT"
            transport["analytics_cache"] = (
                "GeoParquet" if persisted_layer.geoparquet_key else "pending"
            )
            result["layer_id"] = persisted_layer.layer_id
            result["pmtiles_key"] = persisted_layer.pmtiles_key
            result["geoparquet_key"] = persisted_layer.geoparquet_key
            result["pmtiles_maxzoom"] = persisted_layer.pmtiles_maxzoom
            _discard_local_geoparquet_artifact(result)
        else:
            await _upload_geoparquet_artifact(
                result,
                meta=meta,
                source_layer_id=args.layer_id,
                s3_client=s3_client,
            )
            source_id = f"sage-raster-objects-{uuid.uuid4().hex[:8]}"
            style = _candidate_style()
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Rendering review marks preview: {row['name']}",
                bounds=result.get("bbox"),
            ) as payload:
                payload.updates["add_geojson_layer"] = geojson_layer_update(
                    source_id=source_id,
                    geojson=result["geojson"],
                    name=visible_layer_name,
                    bounds=result.get("bbox"),
                    style_hint="raster_object_candidates",
                    style=style,
                )
                await asyncio.sleep(0.2)
            render_engine["source_id"] = source_id
            render_engine["rendered"] = True
            render_engine["style_hint"] = "raster_object_candidates"
            transport["current"] = "inline_geojson_preview_fallback"
    elif result.get("status") == "success":
        await _upload_geoparquet_artifact(
            result,
            meta=meta,
            source_layer_id=args.layer_id,
            s3_client=s3_client,
        )

    if result.get("status") == "success":
        geojson = result["geojson"]
        result["geojson_feature_count"] = len(geojson.get("features", []))
        if persisted_layer:
            result["geojson"] = (
                "omitted from tool response; persisted as PMTiles/MVT layer "
                f"{persisted_layer.layer_id}"
            )
        else:
            result["geojson"] = json.dumps(geojson)

    return result


def _visible_review_layer_name(source_layer_name: str, target_classes: list[str]) -> str:
    normalized = {str(value).strip().lower() for value in target_classes if value}
    if normalized and normalized <= {"building", "house", "roof"}:
        return f"House/Roof Review Marks - {source_layer_name}"
    return f"Feature Review Marks - {source_layer_name}"


async def _run_service_with_timeout(
    payload: RasterObjectCandidateInput,
    timeout_seconds: float,
) -> dict[str, Any]:
    return await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(
            None,
            lambda: analyze_raster_object_candidates_service(payload),
        ),
        timeout=timeout_seconds,
    )


def _timeout_seconds_for_engine(engine_preference: str) -> float:
    engine = str(engine_preference or "").strip().lower()
    env_key = (
        "MUNDI_RASTER_OBJECT_DEEP_TIMEOUT_SECONDS"
        if _should_fallback_after_timeout(engine)
        else "MUNDI_RASTER_OBJECT_TIMEOUT_SECONDS"
    )
    default = "30" if _should_fallback_after_timeout(engine) else "120"
    raw = os.environ.get(env_key, default)
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _should_fallback_after_timeout(engine_preference: str) -> bool:
    engine = str(engine_preference or "").strip().lower()
    return engine in {
        "samgeo",
        "segment-geospatial",
        "segment_geospatial",
        "terramind",
        "terramind_first",
        "terramind_samgeo",
        "terramind_geolibre",
        "geoai_planner",
        "semantic_planner",
    }


def _annotate_timeout_fallback(
    result: dict[str, Any],
    *,
    requested_engine: str,
    timeout_seconds: float,
) -> None:
    summary = result.get("summary")
    if isinstance(summary, dict):
        engine_label = _plain_engine_label(requested_engine)
        reason = (
            f"{engine_label} timed out after {timeout_seconds:.0f}s on this live "
            "request; used the quick raster marker instead."
        )
        summary["deep_pass_fallback_reason"] = reason
        previous = summary.get("performance_note")
        summary["performance_note"] = f"{previous} {reason}".strip() if previous else reason

    engines = result.setdefault("engines", {})
    if isinstance(engines, dict):
        engines["deep_pass_timeout_fallback"] = {
            "requested": requested_engine,
            "used": "rasterio_numpy_candidate_extractor_v2",
            "timeout_seconds": timeout_seconds,
        }


def _plain_engine_label(engine_preference: str) -> str:
    engine = str(engine_preference or "").strip().lower()
    if engine in {
        "terramind",
        "terramind_first",
        "terramind_samgeo",
        "terramind_geolibre",
        "geoai_planner",
        "semantic_planner",
    }:
        return "The deep semantic image pass"
    if engine in {"samgeo", "segment-geospatial", "segment_geospatial"}:
        return "The mask-drawing pass"
    return "The image analysis pass"


def _exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


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


def _discard_local_geoparquet_artifact(result: dict[str, Any]) -> None:
    artifact = result.get("geoparquet")
    if not isinstance(artifact, dict):
        return
    local_path = artifact.get("path")
    if isinstance(local_path, str):
        try:
            os.remove(local_path)
        except OSError:
            pass
    artifact.pop("path", None)
    key = result.get("geoparquet_key")
    if isinstance(key, str):
        artifact["key"] = key
        artifact["storage"] = "s3"
