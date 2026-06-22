from __future__ import annotations

import json

import pytest

from src.services.open_buildings import (
    OpenBuildingsExposureInput,
    analyze_open_buildings_exposure,
    select_open_buildings_tiles_for_bbox,
)


@pytest.fixture(autouse=True)
def _fake_geolibre_open_buildings_conversion(monkeypatch):
    def fake_run(payload):
        assert payload.tool_id == "write_geoparquet"
        assert payload.source_category == "open_buildings"
        return {
            "status": "success",
            "backend": "geolibre_wasm",
            "tool_id": payload.tool_id,
            "tool_source": "geolibre",
            "tool_category": "Conversion",
            "output_file_count": 1,
            "output_bytes": 2048,
            "output_files": [
                {
                    "path": "open_buildings.parquet",
                    "size_bytes": 2048,
                    "media_kind": "geoparquet",
                }
            ],
        }

    monkeypatch.setattr("src.services.geolibre_runner.run_geolibre_tool", fake_run)


def test_analyze_open_buildings_exposure_from_csv_generates_h3_layer():
    csv_text = """latitude,longitude,area_in_meters,confidence,geometry,full_plus_code
-1.9500,30.0600,84.5,0.91,"POLYGON((30.0599 -1.9501,30.0601 -1.9501,30.0601 -1.9499,30.0599 -1.9499,30.0599 -1.9501))",6GCR23XX+XX
-1.9510,30.0610,40.0,0.62,"POLYGON((30.0609 -1.9511,30.0611 -1.9511,30.0611 -1.9509,30.0609 -1.9509,30.0609 -1.9511))",6GCR24XX+XX
-1.9520,30.0620,120.0,0.86,"POLYGON((30.0619 -1.9521,30.0621 -1.9521,30.0621 -1.9519,30.0619 -1.9519,30.0619 -1.9521))",6GCR25XX+XX
"""
    result = analyze_open_buildings_exposure(
        OpenBuildingsExposureInput(
            location_label="Test settlement",
            bbox=[30.055, -1.956, 30.066, -1.945],
            h3_resolution=10,
            min_confidence=0.75,
            buildings_geojson="",
            open_buildings_csv=csv_text,
            risk_factors_json=json.dumps({"rainfall_mm_24h": 72, "drainage_deficit": 0.7}),
            max_buildings=100,
            max_hexes=5000,
            include_ingest_plan=False,
            fetch_tile_metadata=False,
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["building_count"] == 2
    assert result["summary"]["building_area_m2"] == 204.5
    assert result["summary"]["mean_confidence"] == 0.885
    assert result["building_exposure_feature_count"] == 2
    assert result["engines"]["exposure"]["source"] == "Google Open Buildings V3"
    assert result["geolibre_vector_conversion"]["status"] == "success"
    assert result["engines"]["geolibre"]["tool_id"] == "write_geoparquet"
    assert result["geojson"]["features"]
    assert any(
        feature["properties"]["building_count"] > 0
        for feature in result["geojson"]["features"]
    )


def test_analyze_open_buildings_exposure_without_data_returns_ingest_plan():
    result = analyze_open_buildings_exposure(
        OpenBuildingsExposureInput(
            location_label="Needs cache",
            bbox=[30.0, -2.0, 30.1, -1.9],
            h3_resolution=9,
            min_confidence=0.75,
            buildings_geojson="",
            open_buildings_csv="",
            risk_factors_json="",
            max_buildings=100,
            max_hexes=1000,
            include_ingest_plan=True,
            fetch_tile_metadata=False,
        )
    )

    assert result["status"] == "needs_open_buildings_data"
    assert result["summary"]["building_count"] == 0
    assert result["ingest_plan"]["steps"]
    assert result["dataset"]["name"] == "Google Open Buildings V3"
    assert "Rwanda" in result["dataset"]["coverage_note"]


def test_analyze_open_buildings_exposure_skips_malformed_wkt_rows():
    csv_text = """latitude,longitude,area_in_meters,confidence,geometry,full_plus_code
-1.9500,30.0600,84.5,0.91,"POLYGON((30.0599 -1.9501,30.0601 -1.9501,30.0601 -1.9499,30.0599 -1.9499,30.0599 -1.9501))",6GCR23XX+XX
-1.9510,30.0610,40.0,0.95,"not wkt",6GCR24XX+XX
"""
    result = analyze_open_buildings_exposure(
        OpenBuildingsExposureInput(
            location_label="Test settlement",
            bbox=[30.055, -1.956, 30.066, -1.945],
            h3_resolution=10,
            min_confidence=0.75,
            buildings_geojson="",
            open_buildings_csv=csv_text,
            risk_factors_json="",
            max_buildings=100,
            max_hexes=5000,
            include_ingest_plan=False,
            fetch_tile_metadata=False,
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["building_count"] == 1


def test_select_open_buildings_tiles_for_bbox_uses_public_metadata(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [29.9, -2.1],
                                    [30.2, -2.1],
                                    [30.2, -1.8],
                                    [29.9, -1.8],
                                    [29.9, -2.1],
                                ]],
                            },
                            "properties": {
                                "tile_id": "abc",
                                "tile_url": "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip/abc_buildings.csv.gz",
                                "size_mb": 12.3,
                            },
                        },
                        {
                            "type": "Feature",
                            "geometry": {
                                "type": "Polygon",
                                "coordinates": [[
                                    [10.0, 10.0],
                                    [11.0, 10.0],
                                    [11.0, 11.0],
                                    [10.0, 11.0],
                                    [10.0, 10.0],
                                ]],
                            },
                            "properties": {
                                "tile_id": "far",
                                "tile_url": "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip/far_buildings.csv.gz",
                                "size_mb": 5.0,
                            },
                        },
                    ],
                }
            ).encode("utf-8")

    monkeypatch.setattr(
        "src.services.open_buildings.urllib.request.urlopen",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    selected = select_open_buildings_tiles_for_bbox([30.0, -2.0, 30.1, -1.9])

    assert selected == [
        {
            "tile_id": "abc",
            "tile_url": "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_4_gzip/abc_buildings.csv.gz",
            "gcs_url": "gs://open-buildings-data/v3/polygons_s2_level_4_gzip/abc_buildings.csv.gz",
            "size_mb": 12.3,
        }
    ]
