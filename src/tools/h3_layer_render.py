"""Put an H3 risk result on the map: a saved layer when possible, an inline preview otherwise.

The drone-raster context layer (raster_h3_context) draws its hexagons through
``render_h3_risk_layer``. A saved layer
(PMTiles + GeoParquet, see src.services.h3_layer_persistence) survives a page
reload; the inline GeoJSON preview only lives in the open browser tab, so it is
used only when saving fails.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

from src.routes.websocket import kue_ephemeral_action
from src.services.h3_layer_persistence import PersistedH3Layer, persist_h3_layer
from src.services.h3_risk_classes import inline_style_stops, legend
from src.tools.geojson_transport import geojson_layer_update
from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


def inline_h3_style(render_3d: bool, extrusion_scale: float = 35) -> dict[str, Any]:
    """Style for the inline preview, in the same risk classes as saved layers."""
    return {
        "color_property": "risk_score",
        "stops": inline_style_stops(),
        "legend": legend(),
        "fill_opacity": 0.58,
        "stroke_color": "#111827",
        "stroke_width": 1.2,
        "extrude_3d": render_3d,
        "extrusion_property": "risk_score",
        "extrusion_scale": extrusion_scale,
    }


async def render_h3_risk_layer(
    result: dict[str, Any],
    *,
    meta: IngabeToolCallMetaArgs,
    layer_name: str,
    render_3d: bool,
    bounds: Any,
    analysis_kind: str,
    extrusion_scale: float = 35,
    style_hint: str = "h3_spatial_insight_risk",
) -> PersistedH3Layer | None:
    """Draw ``result["geojson"]`` (H3 cells with ``risk_score``) and record how in ``result["engines"]``.

    Returns the saved layer, or None when only the inline preview was sent.
    """
    engines = result.setdefault("engines", {})
    render_engine = engines.setdefault("render", {})
    transport = engines.setdefault("transport", {})

    persisted: PersistedH3Layer | None = None
    try:
        persisted = await persist_h3_layer(
            result=result,
            user_uuid=meta.user_uuid,
            map_id=meta.map_id,
            project_id=meta.project_id,
            layer_name=layer_name,
            render_3d=render_3d,
            analysis_kind=analysis_kind,
        )
    except Exception as exc:  # noqa: BLE001 - the preview still shows the result; logged for follow-up
        logger.warning("Saving H3 layer %r failed; sending an inline preview: %s", layer_name, exc, exc_info=True)

    if persisted:
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Saving {layer_name}",
            layer_id=persisted.layer_id,
            update_style_json=True,
            bounds=persisted.bounds or bounds,
        ) as payload:
            payload.updates["h3_layer_persisted"] = {
                "layer_id": persisted.layer_id,
                "name": layer_name,
                "pmtiles": True,
                "geoparquet": bool(persisted.geoparquet_key),
                "pmtiles_maxzoom": persisted.pmtiles_maxzoom,
                "feature_count": persisted.feature_count,
            }
            await asyncio.sleep(0.2)
        render_engine["layer_id"] = persisted.layer_id
        transport["current"] = "pmtiles_vector_layer"
        transport["browser"] = "PMTiles/MVT"
        transport["analytics_cache"] = "GeoParquet" if persisted.geoparquet_key else "pending"
        result["layer_id"] = persisted.layer_id
        result["pmtiles_key"] = persisted.pmtiles_key
        result["geoparquet_key"] = persisted.geoparquet_key
        result["pmtiles_maxzoom"] = persisted.pmtiles_maxzoom
    else:
        source_id = f"sage-h3-{uuid.uuid4().hex[:8]}"
        async with kue_ephemeral_action(
            meta.conversation_id,
            f"Rendering {layer_name} (preview, not saved)",
            bounds=bounds,
        ) as payload:
            payload.updates["add_geojson_layer"] = geojson_layer_update(
                source_id=source_id,
                geojson=result["geojson"],
                name=layer_name,
                bounds=bounds,
                style_hint=style_hint,
                style=inline_h3_style(render_3d, extrusion_scale),
            )
            await asyncio.sleep(0.2)
        render_engine["source_id"] = source_id
        transport["current"] = "inline_geojson_preview_fallback"
        result["map_note"] = "Shown as a preview only: it will not be on the map after a page reload."
    render_engine["rendered"] = True
    return persisted


def compact_h3_geojson(result: dict[str, Any], persisted: PersistedH3Layer | None) -> None:
    """Shrink the GeoJSON in a successful tool response (the map already has it)."""
    geojson = result["geojson"]
    result["geojson_feature_count"] = len(geojson.get("features", []))
    if persisted:
        result["geojson"] = f"omitted from tool response; persisted as PMTiles/MVT layer {persisted.layer_id}"
    else:
        result["geojson"] = json.dumps(geojson)
