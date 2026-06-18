from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from boto3.s3.transfer import TransferConfig

from src.database.models import LAYER_TYPE_VECTOR
from src.postgis_tiles import MVT_LAYER_NAME
from src.structures import get_async_db_connection
from src.utils import generate_id, get_async_s3_client, get_bucket_name

logger = logging.getLogger(__name__)
one_shot_config = TransferConfig(multipart_threshold=5 * 1024**3)


@dataclass(frozen=True)
class PersistedH3Layer:
    layer_id: str
    style_id: str
    pmtiles_key: str
    geoparquet_key: str | None
    bounds: list[float] | None
    feature_count: int
    geometry_type: str


async def persist_h3_spatial_insight_layer(
    *,
    result: dict[str, Any],
    user_uuid: str,
    map_id: str,
    project_id: str,
    layer_name: str,
    render_3d: bool,
) -> PersistedH3Layer:
    """Persist an H3 analysis as a real map layer.

    GeoJSON is used only as an internal construction format here. The browser
    sees PMTiles for rendering, while GeoParquet is cached for downstream
    analytics and reuse.
    """

    feature_collection = result.get("geojson")
    if not isinstance(feature_collection, dict):
        raise ValueError("H3 result is missing GeoJSON feature collection")
    features = feature_collection.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("H3 result contains no features to persist")

    layer_id = generate_id(prefix="L")
    style_id = generate_id(prefix="S")
    feature_count = len(features)

    with tempfile.TemporaryDirectory() as temp_dir:
        geojson_path = os.path.join(temp_dir, f"{layer_id}.geojson")
        with open(geojson_path, "w", encoding="utf-8") as f:
            json.dump(feature_collection, f, separators=(",", ":"))

        geoparquet_key = await _try_upload_geoparquet_cache(
            feature_collection,
            temp_dir=temp_dir,
            layer_id=layer_id,
            user_uuid=user_uuid,
            project_id=project_id,
        )

        from src.upload.pmtiles import process_vector_layer_common

        processed = await process_vector_layer_common(
            layer_id,
            geojson_path,
            layer_name,
            user_uuid,
            project_id,
        )

    if not processed.pmtiles_key:
        raise RuntimeError("H3 PMTiles generation failed")

    summary = result.get("summary", {}) if isinstance(result.get("summary"), dict) else {}
    zoom_resolution_map = summary.get("zoom_resolution_map")
    metadata = processed.metadata.model_dump(exclude_none=True)
    metadata.update(
        {
            "source": "sage_h3_spatial_insight",
            "analysis_kind": "h3_spatial_insight",
            "screening_model": result.get("screening_model", "h3_spatial_insight_v1"),
            "source_layer_id": summary.get("source_layer_id"),
            "browser_transport": "pmtiles",
            "analytics_format": "geoparquet" if geoparquet_key else "pending",
            "geoparquet_key": geoparquet_key,
            "source_storage_format": "geoparquet" if geoparquet_key else None,
            "geojson_role": "temporary_backend_conversion_only",
            "pmtiles_key": processed.pmtiles_key,
            "h3_resolution": summary.get("h3_resolution"),
            "h3_resolutions": summary.get("h3_resolutions"),
            "adaptive_resolution": summary.get("adaptive_resolution"),
            "resolution_count": summary.get("resolution_count"),
            "resolution_cell_counts": summary.get("resolution_cell_counts"),
            "zoom_resolution_map": zoom_resolution_map,
            "risk_score_property": "risk_score",
        }
    )
    style_json = build_h3_risk_maplibre_layers(
        layer_id,
        render_3d=render_3d,
        zoom_resolution_map=zoom_resolution_map if isinstance(zoom_resolution_map, list) else None,
    )

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
                processed.feature_count,
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

    return PersistedH3Layer(
        layer_id=layer_id,
        style_id=style_id,
        pmtiles_key=processed.pmtiles_key,
        geoparquet_key=geoparquet_key,
        bounds=processed.bounds,
        feature_count=feature_count,
        geometry_type=processed.geometry_type,
    )


async def _try_upload_geoparquet_cache(
    feature_collection: dict[str, Any],
    *,
    temp_dir: str,
    layer_id: str,
    user_uuid: str,
    project_id: str,
) -> str | None:
    try:
        import geopandas as gpd

        parquet_path = os.path.join(temp_dir, f"{layer_id}.parquet")
        gdf = gpd.GeoDataFrame.from_features(
            feature_collection.get("features", []),
            crs="EPSG:4326",
        )
        gdf.to_parquet(parquet_path, index=False)

        geoparquet_key = f"geoparquet/{user_uuid}/{project_id}/{layer_id}.parquet"
        s3 = await get_async_s3_client()
        await s3.upload_file(
            parquet_path,
            get_bucket_name(),
            geoparquet_key,
            Config=one_shot_config,
        )
        return geoparquet_key
    except Exception:
        logger.debug("GeoParquet cache write skipped for H3 layer %s", layer_id, exc_info=True)
        return None


def build_h3_risk_maplibre_layers(
    layer_id: str,
    *,
    render_3d: bool,
    zoom_resolution_map: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    risk_color = [
        "step",
        ["coalesce", ["get", "risk_score"], 0],
        "#22c55e",
        40,
        "#facc15",
        60,
        "#f97316",
        80,
        "#dc2626",
    ]
    base = {
        "source": layer_id,
        "source-layer": MVT_LAYER_NAME,
    }
    zoom_bands = _normalized_zoom_bands(zoom_resolution_map)
    if zoom_bands:
        layers: list[dict[str, Any]] = []
        for band in zoom_bands:
            band_base = {
                **base,
                "filter": ["==", ["get", "h3_resolution"], band["h3_resolution"]],
            }
            if band["minzoom"] > 0:
                band_base["minzoom"] = band["minzoom"]
            if band["maxzoom"] < 24:
                band_base["maxzoom"] = band["maxzoom"]
            resolution_suffix = f"r{band['h3_resolution']}"
            layers.append(
                _h3_risk_fill_layer(
                    band_base,
                    layer_id=layer_id,
                    risk_color=risk_color,
                    render_3d=render_3d,
                    suffix=resolution_suffix,
                )
            )
            layers.append(
                {
                    **band_base,
                    "id": f"h3-risk-outline-{layer_id}-{resolution_suffix}",
                    "type": "line",
                    "paint": {
                        "line-color": "#111827",
                        "line-width": 1.1,
                        "line-opacity": 0.78,
                    },
                }
            )
        return layers

    main_layer = _h3_risk_fill_layer(
        base,
        layer_id=layer_id,
        risk_color=risk_color,
        render_3d=render_3d,
    )

    return [
        main_layer,
        {
            **base,
            "id": f"h3-risk-outline-{layer_id}",
            "type": "line",
            "paint": {
                "line-color": "#111827",
                "line-width": 1.1,
                "line-opacity": 0.78,
            },
        },
    ]


def _h3_risk_fill_layer(
    base: dict[str, Any],
    *,
    layer_id: str,
    risk_color: list[Any],
    render_3d: bool,
    suffix: str | None = None,
) -> dict[str, Any]:
    layer_suffix = f"-{suffix}" if suffix else ""
    if render_3d:
        return {
            **base,
            "id": f"h3-risk-extrusion-{layer_id}{layer_suffix}",
            "type": "fill-extrusion",
            "paint": {
                "fill-extrusion-color": risk_color,
                "fill-extrusion-opacity": 0.62,
                "fill-extrusion-height": ["*", ["coalesce", ["get", "risk_score"], 0], 45],
                "fill-extrusion-base": 0,
            },
        }

    return {
        **base,
        "id": f"h3-risk-fill-{layer_id}{layer_suffix}",
        "type": "fill",
        "paint": {
            "fill-color": risk_color,
            "fill-opacity": 0.58,
        },
    }


def _normalized_zoom_bands(
    zoom_resolution_map: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not zoom_resolution_map:
        return []

    bands: list[dict[str, Any]] = []
    for entry in zoom_resolution_map:
        try:
            resolution = int(entry["h3_resolution"])
            minzoom = float(entry.get("minzoom", 0))
            maxzoom = float(entry.get("maxzoom", 24))
        except (TypeError, ValueError, KeyError):
            continue
        minzoom = max(0.0, min(24.0, minzoom))
        maxzoom = max(0.0, min(24.0, maxzoom))
        if maxzoom <= minzoom:
            continue
        bands.append(
            {
                "h3_resolution": resolution,
                "minzoom": minzoom,
                "maxzoom": maxzoom,
            }
        )
    return bands
