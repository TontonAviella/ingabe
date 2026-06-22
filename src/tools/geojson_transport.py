from __future__ import annotations

import base64
import gzip
import json
from typing import Any


def geojson_layer_update(
    *,
    source_id: str,
    geojson: dict[str, Any],
    name: str,
    bounds: Any = None,
    style_hint: str | None = None,
    style: dict[str, Any] | None = None,
    compress_min_bytes: int = 2048,
) -> dict[str, Any]:
    """Build a websocket GeoJSON layer update with explicit gzip transport.

    GeoParquet remains the analytics artifact for these layers, but MapLibre's
    live source path still expects GeoJSON. Sending gzip+base64 over the JSON
    websocket preserves that fast preview path without shipping large raw JSON
    payloads for H3/candidate layers.
    """

    update: dict[str, Any] = {
        "source_id": source_id,
        "name": name,
        "bounds": bounds,
        "style_hint": style_hint,
        "style": style,
    }
    encoded = json.dumps(geojson, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    update["geojson_raw_size_bytes"] = len(encoded)

    if len(encoded) >= compress_min_bytes:
        compressed = gzip.compress(encoded, compresslevel=6)
        compressed_b64 = base64.b64encode(compressed).decode("ascii")
        if len(compressed_b64) < len(encoded):
            update["geojson_gzip_b64"] = compressed_b64
            update["geojson_encoding"] = "gzip+base64"
            update["geojson_compressed_size_bytes"] = len(compressed)
            update["geojson_transport_size_bytes"] = len(compressed_b64)
            return update

    update["geojson"] = geojson
    update["geojson_encoding"] = "identity"
    update["geojson_transport_size_bytes"] = len(encoded)
    return update
