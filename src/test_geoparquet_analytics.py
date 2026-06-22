"""Tests for GeoParquet as the primary vector analytics store."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("geopandas")
pytest.importorskip("pyarrow")

import geopandas as gpd

from src.duckdb import (
    _metadata_geoparquet_key,
    _run_duckdb_query_from_path,
)
from src.upload.geoparquet import _write_ogr_source_to_geoparquet
from src.upload.models import MetadataUpdates


def _write_sample_geojson(path):
    feature_collection = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"name": "alpha", "risk_score": 12},
                "geometry": {"type": "Point", "coordinates": [30.0, -2.0]},
            },
            {
                "type": "Feature",
                "properties": {"name": "beta", "risk_score": 44},
                "geometry": {"type": "Point", "coordinates": [31.0, -1.0]},
            },
        ],
    }
    path.write_text(json.dumps(feature_collection), encoding="utf-8")


def test_vector_metadata_contract_carries_geoparquet_fields():
    metadata = MetadataUpdates(
        geoparquet_key="geoparquet/user/project/Labc.parquet",
        geoparquet_size_bytes=1234,
        geoparquet_compression="zstd",
        geoparquet_crs="EPSG:4326",
        analytics_format="geoparquet",
        source_storage_format="geoparquet",
        geoanalytics_primary=True,
    )

    dumped = metadata.model_dump(exclude_none=True)

    assert dumped["geoparquet_key"] == "geoparquet/user/project/Labc.parquet"
    assert dumped["analytics_format"] == "geoparquet"
    assert dumped["source_storage_format"] == "geoparquet"
    assert dumped["geoanalytics_primary"] is True


def test_write_ogr_source_to_geoparquet_round_trips_geometry(tmp_path):
    source_path = tmp_path / "sample.geojson"
    parquet_path = tmp_path / "sample.parquet"
    _write_sample_geojson(source_path)

    artifact = _write_ogr_source_to_geoparquet(str(source_path), str(parquet_path))
    gdf = gpd.read_parquet(parquet_path)

    assert artifact.feature_count == 2
    assert artifact.size_bytes > 0
    assert artifact.compression in {"zstd", "default"}
    assert gdf.crs.to_epsg() == 4326
    assert list(gdf["name"]) == ["alpha", "beta"]


def test_write_ogr_source_to_geoparquet_rejects_attribute_only_data(tmp_path, monkeypatch):
    source_path = tmp_path / "attribute_only.geojson"
    parquet_path = tmp_path / "attribute_only.parquet"
    source_path.write_text("{}", encoding="utf-8")
    attribute_only = gpd.GeoDataFrame({"name": ["alpha"]})

    monkeypatch.setattr(
        "src.upload.geoparquet._read_ogr_source",
        lambda _ogr_source, _dataset_layer: attribute_only,
    )

    with pytest.raises(ValueError, match="no active geometry column"):
        _write_ogr_source_to_geoparquet(str(source_path), str(parquet_path))


def test_duckdb_query_prefers_geoparquet_geometry_path(tmp_path):
    source_path = tmp_path / "sample.geojson"
    parquet_path = tmp_path / "sample.parquet"
    _write_sample_geojson(source_path)
    _write_ogr_source_to_geoparquet(str(source_path), str(parquet_path))

    result = _run_duckdb_query_from_path(
        sql_query=(
            'SELECT name, risk_score, ST_AsText(geometry) AS wkt '
            'FROM "LgeoTest" ORDER BY risk_score DESC'
        ),
        layer_id="LgeoTest",
        analytics_path=str(parquet_path),
        source_format="geoparquet",
        start_time=0,
        max_n_rows=25,
    )

    assert result["source_format"] == "geoparquet"
    assert result["headers"] == ["name", "risk_score", "wkt"]
    assert result["result"][0] == ["beta", 44, "POINT (31 -1)"]


def test_metadata_geoparquet_key_accepts_dict_and_json_string():
    expected = "geoparquet/user/project/Labc.parquet"

    assert _metadata_geoparquet_key({"geoparquet_key": expected}) == expected
    assert _metadata_geoparquet_key(json.dumps({"geoparquet_key": expected})) == expected
    assert _metadata_geoparquet_key({"geoparquet_key": ""}) is None
