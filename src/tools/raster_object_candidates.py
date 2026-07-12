from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
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
from src.services.procedural_workflow import (
    ProceduralWorkflow,
    raster_object_workflow,
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
            "Engine preference. Use fastsam for object mask overlays, or "
            "rasterio_numpy for the lightweight raster fallback."
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
    """Find and render basic object review polygons from an uploaded orthophoto.

    Use this for questions like "where are the houses?", "how many likely
    houses are in this orthophoto?", "show roads/trees/playing areas", or
    "detect visible objects in this drone raster." This is the basic raster object
    path that should run before H3 risk cells when the user asks for objects or
    counts. In user-facing replies, describe the result as mask overlays from
    the image. If the live response is capped, say the number is the visible
    overlay size, not the number of objects. Avoid backend/data-source names
    unless the user asks how it works.
    """

    workflow = raster_object_workflow(args.layer_id)

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
        error = f"Layer {args.layer_id} not found."
        workflow.fail("resolve_source", error, layer_id=args.layer_id)
        return _failed_workflow_result(workflow, error, "resolve_source")
    if str(row["owner_uuid"]) != str(meta.user_uuid):
        error = f"Layer {args.layer_id} is not owned by you."
        workflow.fail("resolve_source", error, layer_id=args.layer_id)
        return _failed_workflow_result(workflow, error, "resolve_source")

    workflow.complete(
        "resolve_source",
        layer_id=args.layer_id,
        layer_name=row["name"],
        owner_verified=True,
    )
    if row["type"] != "raster":
        error = f"Layer {args.layer_id} is type '{row['type']}', not a raster."
        workflow.fail("inspect_input", error, layer_type=row["type"])
        return _failed_workflow_result(workflow, error, "inspect_input")

    metadata = (
        json.loads(row["metadata"])
        if isinstance(row["metadata"], str)
        else (dict(row["metadata"]) if row["metadata"] else {})
    )
    source_key = metadata.get("cog_key") or row["s3_key"]
    source_storage = "cog" if metadata.get("cog_key") else "raw_tiff"
    if not source_key:
        error = f"Layer {args.layer_id} has no readable raster object key."
        workflow.fail(
            "inspect_input",
            error,
            layer_type=row["type"],
            source_storage=source_storage,
        )
        return _failed_workflow_result(workflow, error, "inspect_input")

    workflow.complete(
        "inspect_input",
        layer_type=row["type"],
        source_storage=source_storage,
        bounds_available=bool(row["bounds"]),
        target_classes=args.target_classes,
    )
    workflow.complete(
        "plan_analysis",
        engine_requested=args.engine_preference,
        confidence_rule=(
            "> 0.65"
            if _building_only(args.target_classes)
            else f">= {args.confidence_threshold:g}"
        ),
        max_sample_pixels=args.max_sample_pixels,
        min_area_m2=args.min_area_m2,
        max_area_m2=args.max_area_m2,
        max_candidates=args.max_candidates,
        render_map=args.render_map,
    )

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
        result = _timeout_result(
            payload,
            requested_engine=args.engine_preference,
            timeout_seconds=timeout_seconds,
        )
        workflow.fail(
            "execute_analysis",
            result["error"],
            timeout_seconds=timeout_seconds,
        )
        return _attach_failed_workflow(
            result,
            workflow,
            failed_step="execute_analysis",
        )
    except Exception as exc:
        logger.exception(
            "raster object candidate extraction failed for layer %s", args.layer_id
        )
        result = {
            "status": "error",
            "error": (
                f"Raster object candidate extraction failed: {_exception_message(exc)}"
            ),
        }
        workflow.fail("execute_analysis", result["error"])
        return _attach_failed_workflow(
            result,
            workflow,
            failed_step="execute_analysis",
        )

    if result.get("status") != "success":
        error = result.get("error") or result.get("status") or "analysis failed"
        workflow.fail(
            "execute_analysis",
            error,
            engine_requested=args.engine_preference,
        )
        return _attach_failed_workflow(
            result,
            workflow,
            failed_step="execute_analysis",
        )

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    selection = (
        result.get("engines", {}).get("selection", {})
        if isinstance(result.get("engines"), dict)
        else {}
    )
    workflow.complete(
        "execute_analysis",
        engine_requested=args.engine_preference,
        engine_used=selection.get("used") or summary.get("screening_model"),
        candidate_count=summary.get("candidate_count"),
        elapsed_ms=summary.get("elapsed_ms"),
    )

    validation_errors = _validate_candidate_result(result, args)
    if validation_errors:
        error = "Raster object output validation failed: " + "; ".join(validation_errors)
        workflow.fail(
            "validate_output",
            error,
            validation_errors=validation_errors,
        )
        result["status"] = "error"
        result["error"] = error
        return _attach_failed_workflow(
            result,
            workflow,
            failed_step="validate_output",
        )

    workflow.complete(
        "validate_output",
        feature_count=summary.get("candidate_count"),
        class_counts=summary.get("class_counts"),
        confidence_threshold=args.confidence_threshold,
        building_confidence_strictly_above_065=_building_only(args.target_classes),
    )

    persisted_layer = None
    persistence_error: str | None = None
    if result.get("status") == "success":
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
            persistence_error = _exception_message(exc)
            logger.warning(
                "Raster object layer persistence failed; using inline fallback: %s",
                exc,
                exc_info=True,
            )

        if persisted_layer:
            async with kue_ephemeral_action(
                meta.conversation_id,
                f"Saving mask overlay layer: {row['name']}",
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

            workflow.complete(
                "persist_artifacts",
                layer_id=persisted_layer.layer_id,
                feature_count=persisted_layer.feature_count,
                pmtiles_key=persisted_layer.pmtiles_key,
                geoparquet_key=persisted_layer.geoparquet_key,
            )
            delivery = await _verify_persisted_delivery(
                persisted_layer=persisted_layer,
                map_id=meta.map_id,
                user_uuid=meta.user_uuid,
                s3_client=s3_client,
                bucket=get_bucket_name(),
            )
            result["delivery"] = delivery
            if delivery["status"] == "verified":
                workflow.complete("verify_delivery", **delivery["evidence"])
            else:
                workflow.fail(
                    "verify_delivery",
                    delivery.get("error") or "persisted layer delivery was not verified",
                    **delivery.get("evidence", {}),
                )
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
                f"Rendering mask overlay preview: {row['name']}",
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
            workflow.fail(
                "persist_artifacts",
                persistence_error or "persistent map-layer creation did not complete",
                fallback="inline_geojson_preview",
            )
            workflow.skip(
                "verify_delivery",
                "inline preview was queued but cannot be verified as a persistent layer",
            )
            result["delivery"] = {
                "status": "preview_only",
                "verified": False,
                "error": persistence_error,
                "evidence": {"transport": "inline_geojson_preview_fallback"},
            }
    elif result.get("status") == "success":
        await _upload_geoparquet_artifact(
            result,
            meta=meta,
            source_layer_id=args.layer_id,
            s3_client=s3_client,
        )
        artifact = result.get("geoparquet")
        workflow.complete(
            "persist_artifacts",
            render_requested=False,
            geoparquet_key=(
                artifact.get("key") if isinstance(artifact, dict) else None
            ),
        )
        workflow.skip("verify_delivery", "map rendering was not requested")
        result["delivery"] = {
            "status": "not_requested",
            "verified": False,
            "evidence": {"render_map": False},
        }

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

    delivery = result.get("delivery") if isinstance(result.get("delivery"), dict) else {}
    workflow.complete(
        "present_result",
        count_semantics=summary.get("count_semantics"),
        candidate_count=summary.get("candidate_count"),
        visibility_claim=(
            "verified" if delivery.get("status") == "verified" else "not_verified"
        ),
    )
    result["workflow"] = workflow.as_dict()

    return result


def _visible_review_layer_name(
    source_layer_name: str, target_classes: list[str]
) -> str:
    normalized = {str(value).strip().lower() for value in target_classes if value}
    if normalized and normalized <= {"building", "house", "roof"}:
        return f"House/Roof Masks - {source_layer_name}"
    return f"Feature Masks - {source_layer_name}"


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
    engine = str(engine_preference or "").strip().lower().replace("-", "_")
    default = (
        "480"
        if engine in {"auto", "fastsam", "fastsam_s", "ultralytics_fastsam"}
        else "120"
    )
    raw = os.environ.get("MUNDI_RASTER_OBJECT_TIMEOUT_SECONDS", default)
    try:
        return max(5.0, float(raw))
    except (TypeError, ValueError):
        return float(default)


def _timeout_result(
    payload: RasterObjectCandidateInput,
    *,
    requested_engine: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    error = (
        f"Raster object candidate extraction timed out after {timeout_seconds:.0f}s."
    )
    screening_model = "timeout_before_result"
    summary = {
        "source_layer_id": payload.layer_id,
        "source_layer_name": payload.layer_name,
        "candidate_count": 0,
        "candidate_building_count": 0,
        "candidate_count_available": False,
        "candidate_count_capped": False,
        "confirmed_count": False,
        "confirmed_count_available": False,
        "requested_targets": payload.target_classes,
        "count_semantics": "not_available_timeout",
        "timeout_seconds": timeout_seconds,
        "screening_model": screening_model,
        "performance_note": error,
    }
    return {
        "status": "error",
        "error": error,
        "summary": summary,
        "engines": {
            "selection": {
                "requested": requested_engine,
                "used": screening_model,
                "timeout_seconds": timeout_seconds,
            }
        },
    }


def _exception_message(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _failed_workflow_result(
    workflow: ProceduralWorkflow,
    error: str,
    failed_step: str,
) -> dict[str, Any]:
    return _attach_failed_workflow(
        {"status": "error", "error": error},
        workflow,
        failed_step=failed_step,
    )


def _attach_failed_workflow(
    result: dict[str, Any],
    workflow: ProceduralWorkflow,
    *,
    failed_step: str,
) -> dict[str, Any]:
    for step in workflow.as_dict()["steps"]:
        if step["status"] != "pending" or step["step_id"] == "present_result":
            continue
        workflow.skip(
            step["step_id"],
            f"not run because {failed_step} did not complete",
        )
    workflow.complete(
        "present_result",
        outcome="error",
        failed_step=failed_step,
        visibility_claim="not_verified",
    )
    result["workflow"] = workflow.as_dict()
    return result


def _building_only(target_classes: list[str]) -> bool:
    normalized = {
        str(value).strip().lower()
        for value in target_classes
        if str(value).strip()
    }
    return bool(normalized) and normalized <= {"building", "buildings", "house", "houses", "roof", "roofs"}


def _validate_candidate_result(
    result: dict[str, Any],
    args: AnalyzeRasterObjectCandidatesArgs,
) -> list[str]:
    errors: list[str] = []
    geojson = result.get("geojson")
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        return ["result is missing a GeoJSON FeatureCollection"]
    features = geojson.get("features")
    if not isinstance(features, list):
        return ["GeoJSON features is not a list"]

    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    reported_count = summary.get("candidate_count")
    if reported_count is not None and int(reported_count) != len(features):
        errors.append(
            f"summary candidate_count {reported_count} does not match {len(features)} features"
        )

    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
            errors.append(f"feature {index} has no geometry")
            continue
        properties = feature.get("properties")
        if not isinstance(properties, dict):
            errors.append(f"feature {index} has no properties")
            continue
        candidate_class = str(properties.get("candidate_class") or "").strip()
        if not candidate_class:
            errors.append(f"feature {index} has no candidate_class")
        try:
            confidence = float(properties.get("confidence"))
        except (TypeError, ValueError):
            errors.append(f"feature {index} has invalid confidence")
            continue
        if candidate_class == "building" and confidence <= 0.65:
            errors.append(
                f"building feature {index} confidence {confidence:g} is not over 0.65"
            )
        elif candidate_class != "building" and confidence < args.confidence_threshold:
            errors.append(
                f"feature {index} confidence {confidence:g} is below {args.confidence_threshold:g}"
            )
    return errors


async def _verify_persisted_delivery(
    *,
    persisted_layer: Any,
    map_id: str,
    user_uuid: str,
    s3_client: Any,
    bucket: str,
) -> dict[str, Any]:
    from src.structures import get_async_read_connection

    evidence: dict[str, Any] = {
        "layer_id": persisted_layer.layer_id,
        "feature_count_expected": persisted_layer.feature_count,
        "database": False,
        "map_attached": False,
        "style_attached": False,
        "pmtiles": False,
        "geoparquet": not bool(persisted_layer.geoparquet_key),
    }
    try:
        async with get_async_read_connection() as conn:
            row = await conn.fetchrow(
                """
                SELECT ml.feature_count,
                       COALESCE(ml.layer_id = ANY(m.layers), FALSE) AS map_attached,
                       EXISTS (
                           SELECT 1 FROM map_layer_styles mls
                           WHERE mls.map_id = $2 AND mls.layer_id = ml.layer_id
                       ) AS style_attached
                FROM map_layers ml
                LEFT JOIN user_mundiai_maps m ON m.id = $2
                WHERE ml.layer_id = $1 AND ml.owner_uuid = $3
                """,
                persisted_layer.layer_id,
                map_id,
                user_uuid,
            )
        if not row:
            raise RuntimeError("persisted layer is not readable from the database")
        evidence.update(
            {
                "database": True,
                "feature_count_stored": int(row["feature_count"] or 0),
                "map_attached": bool(row["map_attached"]),
                "style_attached": bool(row["style_attached"]),
            }
        )
        if evidence["feature_count_stored"] != persisted_layer.feature_count:
            raise RuntimeError("stored feature count does not match the analysis result")
        if not evidence["map_attached"] or not evidence["style_attached"]:
            raise RuntimeError("persisted layer is not attached to the map and style")

        await s3_client.head_object(Bucket=bucket, Key=persisted_layer.pmtiles_key)
        evidence["pmtiles"] = True
        if persisted_layer.geoparquet_key:
            await s3_client.head_object(
                Bucket=bucket,
                Key=persisted_layer.geoparquet_key,
            )
            evidence["geoparquet"] = True
    except Exception as exc:
        return {
            "status": "unverified",
            "verified": False,
            "error": _exception_message(exc),
            "evidence": evidence,
        }

    return {
        "status": "verified",
        "verified": True,
        "evidence": evidence,
    }


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
