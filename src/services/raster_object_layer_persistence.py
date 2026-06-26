from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from src.database.models import LAYER_TYPE_VECTOR
from src.postgis_tiles import MVT_LAYER_NAME
from src.structures import get_async_db_connection
from src.utils import generate_id

DEFAULT_OBJECT_PMTILES_MAXZOOM = 22
MAX_OBJECT_PMTILES_MAXZOOM = 22
MIN_OBJECT_PMTILES_MAXZOOM = 14


@dataclass(frozen=True)
class PersistedRasterObjectLayer:
    layer_id: str
    style_id: str
    pmtiles_key: str
    geoparquet_key: str | None
    pmtiles_maxzoom: int
    bounds: list[float] | None
    feature_count: int
    geometry_type: str


async def persist_raster_object_candidate_layer(
    *,
    result: dict[str, Any],
    user_uuid: str,
    map_id: str,
    project_id: str,
    layer_name: str,
) -> PersistedRasterObjectLayer:
    """Persist raster object candidates as a real PMTiles vector layer."""

    feature_collection = result.get("geojson")
    if not isinstance(feature_collection, dict):
        raise ValueError("Raster object result is missing GeoJSON feature collection")
    features = feature_collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("Raster object result contains no candidate features to persist")

    layer_id = generate_id(prefix="L")
    style_id = generate_id(prefix="S")
    summary = (
        result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
    )
    engines = result.get("engines") if isinstance(result.get("engines"), dict) else {}
    selection = (
        engines.get("selection")
        if isinstance(engines.get("selection"), dict)
        else {}
    )
    screening_model = (
        result.get("screening_model")
        or summary.get("screening_model")
        or selection.get("used")
        or "raster_object_candidates_v1"
    )
    pmtiles_maxzoom = object_pmtiles_maxzoom()

    with tempfile.TemporaryDirectory() as temp_dir:
        geojson_path = os.path.join(temp_dir, f"{layer_id}.geojson")
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump(feature_collection, f, separators=(",", ":"))

        from src.upload.pmtiles import process_vector_layer_common

        processed = await process_vector_layer_common(
            layer_id,
            geojson_path,
            layer_name,
            user_uuid,
            project_id,
            tippecanoe_maxzoom=pmtiles_maxzoom,
        )

    if not processed.pmtiles_key:
        raise RuntimeError("Raster object PMTiles generation failed")

    geoparquet_key = processed.metadata.geoparquet_key
    feature_count = int(processed.feature_count or len(features))
    metadata = processed.metadata.model_dump(exclude_none=True)
    metadata.update(
        {
            "source": "sage_raster_object_candidates",
            "analysis_kind": "raster_object_candidates",
            "screening_model": screening_model,
            "engine_requested": selection.get("requested"),
            "engine_used": selection.get("used"),
            "performance_note": summary.get("performance_note"),
            "source_layer_id": summary.get("source_layer_id"),
            "source_layer_name": summary.get("source_layer_name"),
            "target_classes": summary.get("requested_targets"),
            "candidate_count": summary.get("candidate_count", feature_count),
            "candidate_building_count": summary.get("candidate_building_count"),
            "candidate_count_capped": summary.get("candidate_count_capped"),
            "max_candidates": summary.get("max_candidates"),
            "confidence_threshold": summary.get("confidence_threshold"),
            "count_semantics": "candidate_screening",
            "count_units": "candidate_polygons",
            "confirmed_count": False,
            "confirmed_count_available": False,
            "browser_transport": "pmtiles",
            "analytics_format": "geoparquet" if geoparquet_key else "pending",
            "geoparquet_key": geoparquet_key,
            "source_storage_format": "geoparquet" if geoparquet_key else None,
            "geojson_role": "temporary_backend_conversion_only",
            "pmtiles_key": processed.pmtiles_key,
            "pmtiles_maxzoom": pmtiles_maxzoom,
            "confidence_property": "confidence",
            "class_property": "candidate_class",
        }
    )
    style_json = build_raster_object_candidate_maplibre_layers(layer_id)

    async with get_async_db_connection() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type,
                 feature_count, source_map_id, s3_key, created_on, last_edited)
                VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7, $8, $9, $10,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                layer_id,
                user_uuid,
                layer_name,
                LAYER_TYPE_VECTOR,
                json.dumps(metadata),
                processed.bounds,
                processed.geometry_type,
                feature_count,
                map_id,
                geoparquet_key,
            )
            await conn.execute(
                """
                INSERT INTO layer_styles (style_id, layer_id, style_json, created_by)
                VALUES ($1, $2, $3::jsonb, $4)
                """,
                style_id,
                layer_id,
                json.dumps(style_json),
                user_uuid,
            )
            await conn.execute(
                """
                INSERT INTO map_layer_styles (map_id, layer_id, style_id)
                VALUES ($1, $2, $3)
                """,
                map_id,
                layer_id,
                style_id,
            )
            map_row = await conn.fetchrow(
                "SELECT layers FROM user_mundiai_maps WHERE id = $1 FOR UPDATE",
                map_id,
            )
            current_layers = list(map_row["layers"] or []) if map_row else []
            if layer_id not in current_layers:
                await conn.execute(
                    """
                    UPDATE user_mundiai_maps
                    SET layers = $1, last_edited = CURRENT_TIMESTAMP
                    WHERE id = $2
                    """,
                    current_layers + [layer_id],
                    map_id,
                )

    return PersistedRasterObjectLayer(
        layer_id=layer_id,
        style_id=style_id,
        pmtiles_key=processed.pmtiles_key,
        geoparquet_key=geoparquet_key,
        pmtiles_maxzoom=pmtiles_maxzoom,
        bounds=processed.bounds,
        feature_count=feature_count,
        geometry_type=processed.geometry_type,
    )


def object_pmtiles_maxzoom() -> int:
    raw_config = os.environ.get("MUNDI_OBJECT_CANDIDATE_PMTILES_MAXZOOM")
    try:
        target = (
            int(raw_config)
            if raw_config is not None
            else DEFAULT_OBJECT_PMTILES_MAXZOOM
        )
    except (TypeError, ValueError):
        target = DEFAULT_OBJECT_PMTILES_MAXZOOM
    return max(MIN_OBJECT_PMTILES_MAXZOOM, min(MAX_OBJECT_PMTILES_MAXZOOM, target))


def build_raster_object_candidate_maplibre_layers(
    layer_id: str,
) -> list[dict[str, Any]]:
    base = {
        "source": layer_id,
        "source-layer": MVT_LAYER_NAME,
    }
    class_color = [
        "match",
        ["get", "candidate_class"],
        "building",
        "#22d3ee",
        "tree_canopy",
        "#22c55e",
        "vegetation_patch",
        "#84cc16",
        "crop_patch",
        "#a3e635",
        "road",
        "#fb923c",
        "linear_boundary",
        "#f97316",
        "bare_rectangle",
        "#eab308",
        "water",
        "#3b82f6",
        "#ec4899",
    ]
    confidence_outline = [
        "step",
        ["coalesce", ["get", "confidence"], 0],
        "#22d3ee",
        0.40,
        "#facc15",
        0.60,
        "#fb923c",
        0.80,
        "#ef4444",
    ]
    return [
        {
            **base,
            "id": f"raster-object-candidates-fill-{layer_id}",
            "type": "fill",
            "paint": {
                "fill-color": class_color,
                "fill-opacity": [
                    "interpolate",
                    ["linear"],
                    ["coalesce", ["get", "confidence"], 0],
                    0,
                    0.45,
                    0.50,
                    0.68,
                    1,
                    0.82,
                ],
                "fill-outline-color": "#0f172a",
            },
        },
        {
            **base,
            "id": f"raster-object-candidates-outline-{layer_id}",
            "type": "line",
            "paint": {
                "line-color": [
                    "case",
                    [">=", ["coalesce", ["get", "confidence"], 0], 0.70],
                    "#ffffff",
                    confidence_outline,
                ],
                "line-width": [
                    "interpolate",
                    ["linear"],
                    ["zoom"],
                    12,
                    1.2,
                    16,
                    2.2,
                    20,
                    3.8,
                ],
                "line-opacity": 0.98,
            },
        },
    ]
