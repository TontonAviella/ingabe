import os
import math
import json
import tempfile
import asyncio
import subprocess
import ipaddress
import socket
import shutil
import logging
from urllib.parse import urlparse
from typing import Optional

from fastapi import HTTPException, status
from fastapi.responses import Response
from boto3.s3.transfer import TransferConfig

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from src.structures import get_async_db_connection, async_conn
from src.utils import get_bucket_name, get_async_s3_client, generate_id, s3_op
from src.upload.models import InternalLayerUploadResponse
from src.dependencies.base_map import BaseMapProvider
from src.postgis_tiles import MVT_LAYER_NAME
from src.database.models import LAYER_TYPE_RASTER, LAYER_TYPE_VECTOR, LAYER_TYPE_POINT_CLOUD, LAYER_TYPE_POSTGIS

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

one_shot_config = TransferConfig(multipart_threshold=5 * 1024**3)  # 5 GiB


def validate_remote_url(url: str, source_type: str) -> str:
    """
    Validate remote URL to prevent SSRF attacks and ensure proper format.

    Args:
        url: The URL to validate
        source_type: Type of source ('vector', 'raster', 'sheets')

    Returns:
        The validated and possibly modified URL

    Raises:
        HTTPException: If URL is invalid or potentially malicious
    """
    # Basic URL format validation
    if source_type == "sheets":
        # CSV sources must have the CSV:/vsicurl/ prefix
        if not url.startswith("CSV:/vsicurl/"):
            raise HTTPException(
                status_code=400,
                detail="Google Sheets URLs must use CSV:/vsicurl/https://... format",
            )
        # Extract the actual URL from CSV:/vsicurl/URL format
        actual_url = url.replace("CSV:/vsicurl/", "")
    elif url.startswith("WFS:"):
        # Extract the actual URL from WFS:URL format
        actual_url = url.replace("WFS:", "")
    elif url.startswith("ESRIJSON:"):
        # Extract the actual URL from ESRIJSON:URL format
        actual_url = url.replace("ESRIJSON:", "")
    else:
        actual_url = url

    # URL must start with http:// or https://
    if not (actual_url.startswith("http://") or actual_url.startswith("https://")):
        raise HTTPException(
            status_code=400, detail="URL must start with http:// or https://"
        )

    try:
        parsed = urlparse(actual_url)
        hostname = parsed.hostname

        if not hostname:
            raise HTTPException(status_code=400, detail="Invalid URL: missing hostname")

        # Resolve hostname to IP address to check for private ranges
        try:
            # Get all IP addresses for the hostname
            addr_info = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            ips = [info[4][0] for info in addr_info]

            for ip_str in ips:
                try:
                    ip = ipaddress.ip_address(ip_str)

                    # Block private IP ranges
                    if ip.is_private:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Access to private IP addresses is not allowed: {ip_str}",
                        )

                    # Block loopback
                    if ip.is_loopback:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Access to loopback addresses is not allowed: {ip_str}",
                        )

                    # Block link-local addresses
                    if ip.is_link_local:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Access to link-local addresses is not allowed: {ip_str}",
                        )

                    # Block multicast
                    if ip.is_multicast:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Access to multicast addresses is not allowed: {ip_str}",
                        )

                    # Block cloud metadata endpoints specifically
                    cloud_metadata_ips = [
                        "169.254.169.254",  # AWS, GCP, Azure metadata
                        "169.254.170.2",  # ECS task metadata
                        "100.100.100.200",  # Alibaba Cloud metadata
                    ]

                    if ip_str in cloud_metadata_ips:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Access to cloud metadata endpoints is not allowed: {ip_str}",
                        )

                except ValueError:
                    # Don't skip invalid IP addresses - reject them
                    raise HTTPException(
                        status_code=400, detail=f"Invalid IP address format: {ip_str}"
                    )

        except socket.gaierror:
            raise HTTPException(
                status_code=400, detail=f"Cannot resolve hostname: {hostname}"
            )

    except Exception as e:
        if isinstance(e, HTTPException):
            raise
        logger.debug("Invalid URL format during validation: %s", e)
        raise HTTPException(status_code=400, detail="Invalid URL format")

    return url


async def internal_upload_layer(
    map_id: str,
    file,
    layer_name: str,
    add_layer_to_map: bool,
    user_id: str,
    project_id: str,
) -> InternalLayerUploadResponse:
    """Internal function to upload a layer without auth checks.

    Uses the Strategy Pattern to dispatch to format-specific handlers.
    See ``src/upload/handlers/`` for handler implementations.
    """
    from src.upload.base import UploadContext
    from src.upload.registry import get_handler, get_layer_type

    async with get_async_db_connection() as conn:
        bucket_name = get_bucket_name()

        filename = file.filename
        file_basename, file_ext = os.path.splitext(filename)
        file_ext = file_ext.lower()

        if not layer_name:
            layer_name = file_basename

        layer_type = get_layer_type(file_ext)
        if not file_ext:
            file_ext = ".tif" if layer_type == LAYER_TYPE_RASTER else ".geojson"

        metadata_dict = {"original_filename": filename}
        layer_id = generate_id(prefix="L")
        s3_key = f"uploads/{user_id}/{project_id}/{layer_id}{file_ext}"
        s3_client = await get_async_s3_client()

        # Stream file to temp disk (never buffer entire file in RAM)
        with tempfile.NamedTemporaryFile(suffix=file_ext) as temp_file:
            file_size_bytes = 0
            chunk_size = 1024 * 1024  # 1 MB
            while chunk := await file.read(chunk_size):
                temp_file.write(chunk)
                file_size_bytes += len(chunk)
            temp_file.flush()
            temp_file_path = temp_file.name

            # Build handler context
            ctx = UploadContext(
                map_id=map_id,
                layer_id=layer_id,
                layer_name=layer_name,
                file_basename=file_basename,
                user_id=user_id,
                project_id=project_id,
                temp_file_path=temp_file_path,
                file_ext=file_ext,
                file_size_bytes=file_size_bytes,
                s3_key=s3_key,
                metadata_dict=metadata_dict,
                conn=conn,
                bucket_name=bucket_name,
            )

            # Phase 1: Format-specific preprocessing (conversion, metadata)
            handler = get_handler(file_ext)
            result = await handler.preprocess(ctx)

            # Apply any path/key/ext overrides from preprocessing
            upload_path = result.updated_temp_file_path or temp_file_path
            upload_key = result.updated_s3_key or s3_key
            ctx.s3_key = upload_key

            # Phase 2: Upload to S3
            await s3_op(s3_client.upload_file(upload_path, bucket_name, upload_key, Config=one_shot_config),
                        "upload", f"layer {ctx.layer_id}")

            # Phase 3 + 4: Create layer rows and update map in a single transaction
            async with conn.transaction():
                result = await handler.create_layers(ctx, result)

                # Phase 4: Update map layer list
                if add_layer_to_map and result.created_layer_ids:
                    map_data = await conn.fetchrow(
                        """
                        SELECT layers FROM user_mundiai_maps
                        WHERE id = $1
                        """,
                        map_id,
                    )
                    current_layers = (
                        map_data["layers"] if map_data and map_data["layers"] else []
                    )
                    await conn.execute(
                        """
                        UPDATE user_mundiai_maps
                        SET layers = $1,
                            last_edited = CURRENT_TIMESTAMP
                        WHERE id = $2
                        """,
                        current_layers + result.created_layer_ids,
                        map_id,
                    )

        # Cleanup temp directories from preprocessing
        if result.temp_dir_to_cleanup:
            shutil.rmtree(result.temp_dir_to_cleanup, ignore_errors=True)

        if not result.created_layer_ids:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "Uploaded vector layer contains no features. "
                    "Please upload a dataset with at least one feature."
                ),
            )

        return InternalLayerUploadResponse(
            id=result.created_layer_ids[0],
            name=result.first_layer_name or (layer_name or file_basename),
            type=result.layer_type,
            url=result.first_layer_url
            or (
                f"/api/layer/{result.created_layer_ids[0]}.pmtiles"
                if result.layer_type == LAYER_TYPE_VECTOR
                else (
                    f"/api/layer/{result.created_layer_ids[0]}.laz"
                    if result.layer_type == LAYER_TYPE_POINT_CLOUD
                    else f"/api/layer/{result.created_layer_ids[0]}.cog.tif"
                )
            ),
        )


async def get_map_style_internal(
    map_id: str,
    base_map: BaseMapProvider,
    only_show_inline_sources: bool = False,
    override_layers: Optional[str] = None,
    basemap: Optional[str] = None,
):
    # Get vector layers for this map from the database
    async with async_conn("get_map_style_internal.fetch_layers") as conn:
        # Get layers and basemap from the map
        map_result = await conn.fetchrow(
            """
            SELECT layers, basemap
            FROM user_mundiai_maps
            WHERE id = $1 AND soft_deleted_at IS NULL
            """,
            map_id,
        )

        if map_result is None:
            raise HTTPException(status_code=404, detail="Map not found")

        # Get layers from the layer list
        layer_ids = map_result["layers"]
        if not layer_ids:
            all_layers = []
        else:
            # Fetch metadata as well to check for cog_url_suffix, and last_edited for cache busting
            all_layers = await conn.fetch(
                """
                SELECT ml.layer_id, ml.name, ml.type, ls.style_json as maplibre_layers, ml.feature_count, ml.bounds, ml.metadata, ml.geometry_type, ml.remote_url, ml.last_edited
                FROM map_layers ml
                LEFT JOIN map_layer_styles mls ON ml.layer_id = mls.layer_id AND mls.map_id = $1
                LEFT JOIN layer_styles ls ON mls.style_id = ls.style_id
                WHERE ml.layer_id = ANY($2)
                ORDER BY ml.id
                """,
                map_id,
                layer_ids,
            )

        vector_layers = [layer for layer in all_layers if layer["type"] == LAYER_TYPE_VECTOR]
        # Filter for raster layers; the .cog.tif endpoint handles generation if needed
        raster_layers = [layer for layer in all_layers if layer["type"] == LAYER_TYPE_RASTER]
        postgis_layers = [layer for layer in all_layers if layer["type"] == LAYER_TYPE_POSTGIS]

        def get_geometry_order(layer):
            geom_type = layer.get("geometry_type") or ""
            geom_type = geom_type.lower()
            if "polygon" in geom_type:
                return 1
            elif "line" in geom_type:
                return 2
            elif "point" in geom_type:
                return 3
            return 4  # ??

        vector_layers.sort(key=get_geometry_order)
        postgis_layers.sort(key=get_geometry_order)

    # Use basemap parameter, or fall back to stored basemap from database.
    # If still None (new map, no stored preference), resolve to the provider's
    # first available style so the metadata always contains a valid string.
    effective_basemap = basemap or map_result["basemap"] or base_map.get_available_styles()[0]
    style_json = await base_map.get_base_style(effective_basemap)

    # Add current basemap to style metadata for frontend
    if "metadata" not in style_json:
        style_json["metadata"] = {}
    style_json["metadata"]["current_basemap"] = effective_basemap

    # compute combined WGS84 bounds from all_layers and derive center + zoom with 20% padding
    bounds_list = [layer["bounds"] for layer in all_layers if layer.get("bounds")]
    ZOOM_PADDING_PCT = 25
    if bounds_list:
        xs = [b[0] for b in bounds_list] + [b[2] for b in bounds_list]
        ys = [b[1] for b in bounds_list] + [b[3] for b in bounds_list]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        # Antimeridian handling: if the naive longitude span exceeds 180°,
        # the bounds likely cross the antimeridian.  Shift negative longitudes
        # to [0, 360) range, re-compute, then normalise back to [-180, 180].
        naive_lon_span = max_x - min_x
        if naive_lon_span > 180:
            xs_shifted = [(lng % 360) for lng in xs]
            min_x, max_x = min(xs_shifted), max(xs_shifted)

        # apply 1/2 padding on each side
        pad_x = (max_x - min_x) * ZOOM_PADDING_PCT / 100
        pad_y = (max_y - min_y) * ZOOM_PADDING_PCT / 100
        min_x -= pad_x
        max_x += pad_x
        min_y -= pad_y
        max_y += pad_y

        # Normalise longitude back to [-180, 180]
        center_x = (min_x + max_x) / 2
        if center_x > 180:
            center_x -= 360
        center_y = (min_y + max_y) / 2

        # final bounds and center
        style_json["center"] = [center_x, center_y]
        # calculate zoom to fit both longitude and latitude spans
        lon_span = max_x - min_x
        lat_span = max_y - min_y
        zoom_lon = math.log2(360.0 / lon_span) if lon_span else None
        zoom_lat = math.log2(180.0 / lat_span) if lat_span else None
        # use the smaller zoom level to ensure both dimensions fit
        zoom = (
            min(zoom_lon, zoom_lat) if zoom_lon and zoom_lat else zoom_lon or zoom_lat
        )
        if zoom is not None and zoom > 0.0:
            style_json["zoom"] = zoom

    if override_layers is not None:
        override_layers = json.loads(override_layers)

    # If no sources in the style, initialize it
    if "sources" not in style_json:
        style_json["sources"] = {}

    if only_show_inline_sources:
        for layer in raster_layers:
            layer_id = layer["layer_id"]
            metadata = json.loads(layer.get("metadata", "{}"))

            # WorldCover layers use the dedicated tile endpoint
            if metadata.get("worldcover"):
                wc_mode = metadata.get("worldcover_mode", "all")
                source_id = f"worldcover-source-{layer_id}"
                tile_url = f"/api/worldcover/{{z}}/{{x}}/{{y}}.png?mode={wc_mode}"
                # Append admin clip params if present (district/sector/cell)
                if metadata.get("clip_district"):
                    tile_url += f"&district={metadata['clip_district']}"
                if metadata.get("clip_sector"):
                    tile_url += f"&sector={metadata['clip_sector']}"
                if metadata.get("clip_cell"):
                    tile_url += f"&cell={metadata['clip_cell']}"
                if metadata.get("clip_bbox"):
                    _cb = metadata["clip_bbox"]
                    tile_url += f"&bbox={_cb[0]},{_cb[1]},{_cb[2]},{_cb[3]}"
                style_json["sources"][source_id] = {
                    "type": "raster",
                    "tiles": [tile_url],
                    "tileSize": 256,
                    "minzoom": 0,
                    "maxzoom": 14,  # Cap at z14 - WorldCover is 10m resolution, z16+ causes timeouts
                }
                style_json["layers"].append(
                    {
                        "id": f"raster-layer-{layer_id}",
                        "type": "raster",
                        "source": source_id,
                        "paint": {"raster-opacity": 0.85},
                    }
                )
                continue

            source_id = f"raster-source-{layer_id}"
            # Add cache-busting parameter using last_edited timestamp
            cache_param = f"v={int(layer['last_edited'].timestamp())}" if layer.get('last_edited') else ""
            tile_url = f"/api/layer/{layer_id}/{{z}}/{{x}}/{{y}}.png"
            if cache_param:
                tile_url += f"?{cache_param}"

            style_json["sources"][source_id] = {
                "type": "raster",
                "tiles": [tile_url],
                "tileSize": 256,
                "minzoom": 0,
                "maxzoom": 22,
            }
            style_json["layers"].append(
                {
                    "id": f"raster-layer-{layer_id}",
                    "type": "raster",
                    "source": source_id,
                }
            )
    else:
        for idx, layer in enumerate(raster_layers, 1):
            layer_id = layer["layer_id"]
            metadata = json.loads(layer.get("metadata", "{}"))

            # WorldCover layers use the dedicated tile endpoint
            if metadata.get("worldcover"):
                wc_mode = metadata.get("worldcover_mode", "all")
                source_id = f"worldcover-source-{layer_id}"
                tile_url = f"/api/worldcover/{{z}}/{{x}}/{{y}}.png?mode={wc_mode}"
                # Append admin clip params if present (district/sector/cell)
                if metadata.get("clip_district"):
                    tile_url += f"&district={metadata['clip_district']}"
                if metadata.get("clip_sector"):
                    tile_url += f"&sector={metadata['clip_sector']}"
                if metadata.get("clip_cell"):
                    tile_url += f"&cell={metadata['clip_cell']}"
                if metadata.get("clip_bbox"):
                    _cb = metadata["clip_bbox"]
                    tile_url += f"&bbox={_cb[0]},{_cb[1]},{_cb[2]},{_cb[3]}"
                style_json["sources"][source_id] = {
                    "type": "raster",
                    "tiles": [tile_url],
                    "tileSize": 256,
                    "minzoom": 0,
                    "maxzoom": 14,  # Cap at z14 - WorldCover is 10m resolution, z16+ causes timeouts
                }
                style_json["layers"].append(
                    {
                        "id": f"raster-layer-{layer_id}",
                        "type": "raster",
                        "source": source_id,
                        "paint": {"raster-opacity": 0.85},
                    }
                )
                continue

            source_id = f"cog-source-{layer_id}"
            cog_url = f"cog:///api/layer/{layer_id}.cog.tif"

            # Generate suffix from raster_value_stats_b1
            if metadata and "raster_value_stats_b1" in metadata:
                min_val = metadata["raster_value_stats_b1"]["min"]
                max_val = metadata["raster_value_stats_b1"]["max"]
                cog_url += f"#color:BrewerSpectral9,{min_val},{max_val},c"

            style_json["sources"][source_id] = {
                "type": "raster",
                "url": cog_url,
                "tileSize": 256,
            }
            style_json["layers"].append(
                {
                    "id": f"raster-layer-{layer_id}",
                    "type": "raster",
                    "source": source_id,
                }
            )

    # Pre-generate all presigned URLs in parallel (avoids sequential S3 calls)
    presigned_urls: dict[str, str] = {}
    if only_show_inline_sources:
        layers_needing_presigned: list[tuple[str, str]] = []
        for layer in vector_layers:
            if not layer["remote_url"]:
                metadata = json.loads(layer.get("metadata", "{}"))
                pmtiles_key = metadata.get("pmtiles_key")
                if pmtiles_key:
                    layers_needing_presigned.append((layer["layer_id"], pmtiles_key))

        if layers_needing_presigned:
            bucket_name = get_bucket_name()
            s3_client = await get_async_s3_client()

            async def _gen_url(key: str) -> str:
                return await s3_op(
                    s3_client.generate_presigned_url("get_object", Params={"Bucket": bucket_name, "Key": key}, ExpiresIn=180),
                    "presigned URL", f"PMTiles {key}",
                )

            urls = await asyncio.gather(*[_gen_url(k) for _, k in layers_needing_presigned])
            for (lid, _), url in zip(layers_needing_presigned, urls):
                presigned_urls[lid] = url

    # Add vector layers as sources and layers to the style
    for idx, layer in enumerate(vector_layers, 1):
        layer_id = layer["layer_id"]

        if layer_id in presigned_urls:
            style_json["sources"][layer_id] = {
                "type": "vector",
                "url": f"pmtiles://{presigned_urls[layer_id]}",
            }
        elif only_show_inline_sources and not layer["remote_url"]:
            # Fallback: pmtiles_key was missing — should not happen
            metadata = json.loads(layer.get("metadata", "{}"))
            pmtiles_key = metadata.get("pmtiles_key")
            assert pmtiles_key is not None, f"Missing pmtiles_key for layer {layer_id}"

            bucket_name = get_bucket_name()
            s3_client = await get_async_s3_client()
            presigned_url = await s3_op(
                s3_client.generate_presigned_url("get_object", Params={"Bucket": bucket_name, "Key": pmtiles_key}, ExpiresIn=180),
                "presigned URL", f"PMTiles {pmtiles_key}",
            )
            style_json["sources"][layer_id] = {
                "type": "vector",
                "url": f"pmtiles://{presigned_url}",
            }
        else:
            # Default to PMTiles
            style_json["sources"][layer_id] = {
                "type": "vector",
                "url": f"pmtiles:///api/layer/{layer_id}.pmtiles",
            }

        # Check if override_layers is not None
        if override_layers is not None and layer_id in override_layers:
            for ml_layer in override_layers[layer_id]:
                # source-layer is prohibited for geojson sources
                if style_json["sources"][layer_id]["type"] == "geojson":
                    assert ml_layer["source-layer"] == MVT_LAYER_NAME
                    del ml_layer["source-layer"]
                    assert "source-layer" not in ml_layer
                style_json["layers"].append(ml_layer)
        # Use stored style_json from layer_styles if no override_layers
        elif layer["maplibre_layers"]:
            for ml_layer in json.loads(layer["maplibre_layers"]):
                style_json["layers"].append(ml_layer)

    for layer in postgis_layers:
        if layer["type"] == LAYER_TYPE_POSTGIS:
            layer_id = layer["layer_id"]

            # Add cache-busting parameter using last_edited timestamp
            cache_param = f"v={int(layer['last_edited'].timestamp())}" if layer.get('last_edited') else ""
            tile_url = f"/api/layer/{layer_id}/{{z}}/{{x}}/{{y}}.mvt"
            if cache_param:
                tile_url += f"?{cache_param}"

            style_json["sources"][layer_id] = {
                "type": "vector",
                "tiles": [tile_url],
                "minzoom": 0,
                "maxzoom": 18,
            }

            # Check if override_layers is not None
            if override_layers is not None and layer_id in override_layers:
                for ml_layer in override_layers[layer_id]:
                    style_json["layers"].append(ml_layer)
            # Use stored style_json from layer_styles if no override_layers
            elif layer["maplibre_layers"]:
                for ml_layer in json.loads(layer["maplibre_layers"]):
                    style_json["layers"].append(ml_layer)

    # ── Inject persisted paint overrides (choropleth, color, opacity) ──────
    # These are saved per (map_id, layer_id) via the PATCH overrides endpoint.
    # Injecting them here ensures ALL users see choropleth colors on map load.
    async with async_conn("get_map_style_internal.paint_overrides") as conn:
        override_rows = await conn.fetch(
            "SELECT layer_id, overrides_json FROM layer_paint_overrides WHERE map_id = $1",
            map_id,
        )
    if override_rows:
        _OPACITY_PROP = {
            "fill": "fill-opacity", "line": "line-opacity", "circle": "circle-opacity",
            "symbol": "icon-opacity", "raster": "raster-opacity",
            "fill-extrusion": "fill-extrusion-opacity", "heatmap": "heatmap-opacity",
        }
        _COLOR_PROP = {"fill": "fill-color", "line": "line-color", "circle": "circle-color"}

        overrides_by_layer = {}
        for row in override_rows:
            ov = row["overrides_json"] if isinstance(row["overrides_json"], dict) else json.loads(row["overrides_json"])
            overrides_by_layer[row["layer_id"]] = ov

        for sl in style_json.get("layers", []):
            src = sl.get("source")
            if not src:
                continue
            # Match layer by source name (same logic as frontend injectOverridesIntoStyle)
            ov = overrides_by_layer.get(src)
            if not ov:
                # Check suffix match
                for lid, candidate in overrides_by_layer.items():
                    if src.endswith(f"-{lid}"):
                        ov = candidate
                        break
            if not ov:
                continue

            paint = sl.setdefault("paint", {})
            layer_type = sl.get("type", "")

            if ov.get("opacity") is not None:
                prop = _OPACITY_PROP.get(layer_type)
                if prop:
                    paint[prop] = ov["opacity"]

            if layer_type == "fill" and ov.get("choroplethExpression") is not None:
                paint["fill-color"] = ov["choroplethExpression"]
            elif ov.get("color") is not None:
                prop = _COLOR_PROP.get(layer_type)
                if prop:
                    paint[prop] = ov["color"]

    # We use globe
    style_json["projection"] = {
        "type": "globe",
    }

    # Add pointer positions source and layers for real-time collaboration
    style_json["sources"]["pointer-positions"] = {
        "type": "geojson",
        "data": {"type": "FeatureCollection", "features": []},
    }

    # label layers should be higher z-index than geometry layers. maintain order otherwise
    non_symbol_layers = [
        layer for layer in style_json["layers"] if layer.get("type") != "symbol"
    ]
    symbol_layers = [
        layer for layer in style_json["layers"] if layer.get("type") == "symbol"
    ]
    style_json["layers"] = non_symbol_layers + symbol_layers

    # Add cursor layer
    style_json["layers"].append(
        {
            "id": "pointer-cursors",
            "type": "symbol",
            "source": "pointer-positions",
            "layout": {
                "icon-image": "remote-cursor",
                "icon-size": 0.45,
                "icon-allow-overlap": True,
            },
        }
    )

    # Add labels layer
    style_json["layers"].append(
        {
            "id": "pointer-labels",
            "type": "symbol",
            "source": "pointer-positions",
            "layout": {
                "text-field": ["get", "abbrev"],
                "text-offset": [1, 1],
                "text-anchor": "top-left",
                "text-size": 11,
                "text-allow-overlap": True,
                "text-ignore-placement": True,
            },
            "paint": {
                "text-color": "#000000",
                "text-halo-color": "#FFFFFF",
                "text-halo-width": 1,
            },
        }
    )

    # Return the augmented style
    return style_json


async def pull_bounds_from_map(map_id: str) -> tuple[float, float, float, float]:
    """Pull the bounds from the map in the database by taking the min and max of all layer bounds."""
    async with get_async_db_connection() as conn:
        result = await conn.fetchrow(
            """
            SELECT
                MIN(ml.bounds[1]) as xmin,
                MIN(ml.bounds[2]) as ymin,
                MAX(ml.bounds[3]) as xmax,
                MAX(ml.bounds[4]) as ymax
            FROM map_layers ml
            JOIN user_mundiai_maps m ON ml.layer_id = ANY(m.layers)
            WHERE m.id = $1 AND ml.bounds IS NOT NULL
            """,
            map_id,
        )

        if not result or result["xmin"] is None:
            # No layers with bounds found
            return (-180, -90, 180, 90)

        return (
            result["xmin"],
            result["ymin"],
            result["xmax"],
            result["ymax"],
        )


# requires style.json to be provided, so that we can do this without auth
async def render_map_internal(
    map_id: str,
    bbox: Optional[str],
    width: int,
    height: int,
    renderer: str,
    bgcolor: str,
    style_json: str,
) -> tuple[Response, dict]:
    if bbox is None:
        xmin, ymin, xmax, ymax = await pull_bounds_from_map(map_id)
    else:
        xmin, ymin, xmax, ymax = map(float, bbox.split(","))

    assert style_json is not None
    # Create a temporary file for the output PNG
    with tempfile.NamedTemporaryFile(suffix=".png") as temp_output:
        output_path = temp_output.name

        # Format the style JSON with required parameters
        input_data = {
            "width": width,
            "height": height,
            "bounds": f"{xmin},{ymin},{xmax},{ymax}",
            "style": style_json,
            "ratio": 1,
        }

        # Get zoom and center for metadata using the zoom script
        zoom_process = await asyncio.create_subprocess_exec(
            "node",
            "src/renderer/zoom.js",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        zoom_stdout, zoom_stderr = await zoom_process.communicate(
            input=json.dumps(
                {
                    "bbox": f"{xmin},{ymin},{xmax},{ymax}",
                    "width": width,
                    "height": height,
                }
            ).encode()
        )
        zoom_data = json.loads(zoom_stdout.decode())

        # Run the renderer using subprocess
        try:
            with tracer.start_as_current_span("renderer.mbgl") as span:
                process = await asyncio.create_subprocess_exec(
                    "xvfb-run",
                    "-a",
                    "node",
                    "src/renderer/render.js",
                    output_path,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )

                stdout, stderr = await process.communicate(
                    input=json.dumps(input_data).encode()
                )

                def _iter_json_lines(buf: bytes):
                    for line in buf.decode(errors="ignore").splitlines():
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        yield obj

                for m in list(_iter_json_lines(stdout)) + list(
                    _iter_json_lines(stderr)
                ):
                    if isinstance(m, dict):
                        sev = str(m.get("severity", "")).upper()
                        text_val = m.get("text")
                        if sev and sev != "INFO":
                            if sev == "WARNING":
                                try:
                                    logger.warning("Renderer warning: %s", text_val)
                                except Exception as e:
                                    logger.warning("Failed to log renderer warning: %s", e)
                            elif sev == "ERROR":
                                try:
                                    span.record_exception(
                                        RuntimeError(text_val or "renderer error")
                                    )
                                except Exception as e:
                                    logger.warning("Failed to record span exception: %s", e)
                                try:
                                    span.set_status(
                                        Status(
                                            StatusCode.ERROR,
                                            text_val or "renderer error",
                                        )
                                    )
                                except Exception as e:
                                    logger.warning("Failed to set span error status: %s", e)

                if process.returncode != 0:
                    raise subprocess.CalledProcessError(
                        process.returncode or -1,
                        "xvfb-run",
                        output=stdout,
                        stderr=stderr,
                    )

            temp_output.seek(0)
            screenshot_data = temp_output.read()

            return (
                Response(
                    content=screenshot_data,
                    media_type="image/png",
                    headers={
                        "Content-Type": "image/png",
                        "Content-Disposition": f"inline; filename=map_{map_id}.png",
                    },
                ),
                zoom_data,
            )
        except subprocess.CalledProcessError as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Error rendering map: {e.stderr.decode()}",
            )
