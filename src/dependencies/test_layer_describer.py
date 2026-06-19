from src.dependencies.layer_describer import (
    DefaultLayerDescriber,
    _coerce_layer_metadata,
    _is_geoparquet_backed_vector,
)


def test_geoparquet_vector_layer_uses_metadata_description() -> None:
    layer_data = {
        "bounds": [30.4167009, -1.7020112, 30.4328292, -1.6917102],
        "s3_key": "geoparquet/user/project/layer.parquet",
        "metadata": {
            "analytics_format": "geoparquet",
            "geoparquet_key": "geoparquet/user/project/layer.parquet",
            "pmtiles_key": "pmtiles/user/project/layer.pmtiles",
            "pmtiles_maxzoom": 20,
            "h3_resolutions": [10, 11, 12],
            "resolution_cell_counts": {"10": 91, "11": 550, "12": 3580},
            "source_layer_id": "raster-layer",
            "screening_model": "raster_h3_context_v1",
            "domain": "mixed",
        },
    }

    metadata = _coerce_layer_metadata(layer_data)

    assert _is_geoparquet_backed_vector(layer_data, metadata) is True

    description = "\n".join(
        DefaultLayerDescriber().describe_vector_layer_from_metadata(
            layer_data, metadata
        )
    )

    assert "Driver: GeoParquet metadata summary" in description
    assert "Analytics Store: GeoParquet" in description
    assert "Browser Transport: PMTiles" in description
    assert "PMTiles Max Zoom: 20" in description
    assert "H3 Resolutions: 10, 11, 12" in description
    assert "Resolution Cell Counts: r10: 91, r11: 550, r12: 3580" in description
