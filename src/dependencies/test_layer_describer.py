from src.dependencies.layer_describer import (
    DefaultLayerDescriber,
    _coerce_layer_metadata,
    _describe_geoparquet_file,
    _describe_ogr_vector_file,
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


def test_geoparquet_vector_layer_uses_pyarrow_and_duckdb_description(tmp_path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    parquet_path = tmp_path / "sample.parquet"
    gdf = gpd.GeoDataFrame(
        {"name": ["alpha", "beta"], "risk_score": [12, 44]},
        geometry=[Point(30.0, -2.0), Point(31.0, -1.0)],
        crs="EPSG:4326",
    )
    gdf.to_parquet(parquet_path, index=False)

    description = "\n".join(
        _describe_geoparquet_file(
            str(parquet_path),
            {
                "bounds": [30.0, -2.0, 31.0, -1.0],
            },
            {
                "analytics_format": "geoparquet",
                "geoparquet_key": "geoparquet/user/project/layer.parquet",
                "pmtiles_key": "pmtiles/user/project/layer.pmtiles",
                "pmtiles_maxzoom": 20,
            },
        )
    )

    assert "Driver: GeoParquet" in description
    assert "GeoParquet Reader: PyArrow" in description
    assert "Query Engine: DuckDB read_parquet" in description
    assert "Row Count: 2" in description
    assert "name: string" in description
    assert "risk_score: int64" in description
    assert "alpha" in description


def test_ogr_vector_layer_uses_pyogrio_description(tmp_path) -> None:
    import json

    geojson_path = tmp_path / "sample.geojson"
    geojson_path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"name": "alpha"},
                        "geometry": {"type": "Point", "coordinates": [30.0, -2.0]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    description, feature_count = _describe_ogr_vector_file(
        str(geojson_path),
        {"bounds": None, "feature_count": None, "geometry_type": None},
    )
    joined = "\n".join(description)

    assert feature_count == 1
    assert "Driver: pyogrio/GDAL" in joined
    assert "Detected Geometry Type: point" in joined
    assert "name: object" in joined
    assert "alpha" in joined
