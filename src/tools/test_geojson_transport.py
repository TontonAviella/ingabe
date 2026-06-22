from __future__ import annotations

import base64
import gzip
import json

from src.tools.geojson_transport import geojson_layer_update


def test_geojson_layer_update_uses_gzip_when_smaller() -> None:
    geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[30, -2], [30.001, -2], [30.001, -2.001], [30, -2.001], [30, -2]]],
                },
                "properties": {"risk_score": 55, "label": "candidate"},
            }
            for _ in range(100)
        ],
    }

    update = geojson_layer_update(
        source_id="sage-test",
        geojson=geojson,
        name="Compressed Layer",
        compress_min_bytes=1,
    )

    assert update["geojson_encoding"] == "gzip+base64"
    assert "geojson" not in update
    decoded = gzip.decompress(base64.b64decode(update["geojson_gzip_b64"])).decode("utf-8")
    assert json.loads(decoded) == geojson
    assert update["geojson_transport_size_bytes"] < update["geojson_raw_size_bytes"]


def test_geojson_layer_update_keeps_identity_when_tiny() -> None:
    geojson = {"type": "FeatureCollection", "features": []}

    update = geojson_layer_update(
        source_id="sage-test",
        geojson=geojson,
        name="Tiny Layer",
    )

    assert update["geojson_encoding"] == "identity"
    assert update["geojson"] == geojson
