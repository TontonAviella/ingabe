from __future__ import annotations

import os

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin

from src.services.raster_object_candidates import (
    RasterObjectCandidateInput,
    analyze_raster_object_candidates,
)


def test_analyze_raster_object_candidates_extracts_compact_buildings(tmp_path) -> None:
    path = tmp_path / "synthetic_ortho.tif"
    image = np.zeros((3, 120, 120), dtype=np.uint8)
    image[0, :, :] = 55
    image[1, :, :] = 145
    image[2, :, :] = 65

    # Compact roof-like rectangles.
    image[:, 12:24, 12:25] = np.array([235, 232, 220], dtype=np.uint8)[:, None, None]
    image[:, 35:51, 72:88] = np.array([210, 214, 225], dtype=np.uint8)[:, None, None]
    image[:, 82:102, 28:45] = np.array([190, 188, 178], dtype=np.uint8)[:, None, None]

    # Road-like stripe. It is bright but too elongated to be a building.
    image[:, 58:64, :] = np.array([205, 175, 120], dtype=np.uint8)[:, None, None]

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=120,
        height=120,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 120, 1, 1),
    ) as ds:
        ds.write(image)

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lsynthetic",
            layer_name="Synthetic Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=20,
            max_sample_pixels=20_000,
            min_area_m2=20,
            max_area_m2=600,
            confidence_threshold=0.25,
            engine_preference="rasterio_numpy",
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["candidate_count"] >= 3
    assert result["summary"]["max_candidates"] == 20
    assert result["summary"]["candidate_count_capped"] is False
    assert result["summary"]["class_counts"]["building"] >= 3
    assert result["summary"]["analytics_format"] == "geoparquet"
    assert result["summary"]["geojson_role"] == "live_map_transport_only"
    assert result["summary"]["live_preview_transport"] == "gzip_geojson_when_smaller"
    assert result["summary"]["count_semantics"] == "candidate_screening"
    assert result["summary"]["count_units"] == "candidate_polygons"
    assert result["summary"]["confirmed_count"] is False
    assert result["summary"]["confirmed_count_available"] is False
    assert result["summary"]["confirmed_building_count"] is None
    assert result["summary"]["candidate_building_count"] >= 3
    assert result["geoparquet"]["role"] == "primary_analytics_store"
    assert os.path.exists(result["geoparquet"]["path"])
    stored = gpd.read_parquet(result["geoparquet"]["path"])
    assert len(stored) == result["summary"]["candidate_count"]
    assert set(stored["candidate_class"]) == {"building"}
    assert result["geojson"]["features"]
    assert all(
        feature["properties"]["candidate_class"] == "building"
        for feature in result["geojson"]["features"]
    )
    assert all(
        feature["properties"]["aspect_ratio"] <= 5.5
        for feature in result["geojson"]["features"]
    )

    capped_result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lsynthetic",
            layer_name="Synthetic Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=1,
            max_sample_pixels=20_000,
            min_area_m2=20,
            max_area_m2=600,
            confidence_threshold=0.25,
            engine_preference="rasterio_numpy",
        )
    )

    assert capped_result["summary"]["candidate_count"] == 1
    assert capped_result["summary"]["pre_cap_candidate_count"] >= 3
    assert capped_result["summary"]["candidate_count_capped"] is True


def test_analyze_raster_object_candidates_can_screen_road_like_segments(tmp_path) -> None:
    path = tmp_path / "synthetic_roads.tif"
    image = np.zeros((3, 140, 140), dtype=np.uint8)
    image[0, :, :] = 55
    image[1, :, :] = 135
    image[2, :, :] = 65

    # Two elongated low-vegetation tracks.
    image[:, 40:47, 8:132] = np.array([205, 175, 120], dtype=np.uint8)[:, None, None]
    image[:, 82:90, 20:120] = np.array([188, 168, 130], dtype=np.uint8)[:, None, None]

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=140,
        height=140,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 140, 1, 1),
    ) as ds:
        ds.write(image)

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lroad",
            layer_name="Synthetic Road Orthophoto",
            bounds_wgs84=None,
            target_classes=["road"],
            max_candidates=10,
            max_sample_pixels=30_000,
            min_area_m2=20,
            max_area_m2=2000,
            confidence_threshold=0.25,
            engine_preference="rasterio_numpy",
        )
    )

    assert result["status"] == "success"
    assert result["summary"]["class_counts"]["road"] >= 1
    assert result["summary"]["analytics_format"] == "geoparquet"
    assert result["geoparquet"]["feature_count"] == result["summary"]["candidate_count"]
    assert all(
        feature["properties"]["candidate_class"] == "road"
        for feature in result["geojson"]["features"]
    )


def test_analyze_raster_object_candidates_splits_land_pattern_targets(tmp_path) -> None:
    path = tmp_path / "synthetic_land_patterns.tif"
    image = np.zeros((3, 180, 180), dtype=np.uint8)
    image[0, :, :] = 82
    image[1, :, :] = 116
    image[2, :, :] = 70

    # A compact bright roof.
    image[:, 18:32, 18:34] = np.array([232, 232, 218], dtype=np.uint8)[:, None, None]

    # A long dirt road.
    image[:, 58:66, 8:172] = np.array([205, 176, 122], dtype=np.uint8)[:, None, None]

    # Textured tree canopy block.
    for row in range(92, 134):
        for col in range(18, 62):
            shade = 25 if (row + col) % 5 < 2 else 0
            image[:, row, col] = np.array([42, 142 + shade, 48], dtype=np.uint8)

    # Smooth crop/field patch.
    image[:, 96:152, 94:160] = np.array([58, 168, 68], dtype=np.uint8)[:, None, None]

    # Sharp field boundary/track line.
    image[:, 152:156, 78:170] = np.array([220, 210, 155], dtype=np.uint8)[:, None, None]

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=180,
        height=180,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 180, 1, 1),
    ) as ds:
        ds.write(image)

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lpatterns",
            layer_name="Synthetic Pattern Orthophoto",
            bounds_wgs84=None,
            target_classes=[
                "building",
                "road",
                "tree",
                "crop",
                "field_boundary",
            ],
            max_candidates=40,
            max_sample_pixels=60_000,
            min_area_m2=8,
            max_area_m2=10_000,
            confidence_threshold=0.20,
            engine_preference="rasterio_numpy",
        )
    )

    assert result["status"] == "success"
    assert result["engines"]["selection"]["requested"] == "rasterio_numpy"
    assert (
        result["engines"]["selection"]["used"]
        == "rasterio_numpy_candidate_extractor_v2"
    )
    assert (
        result["summary"]["screening_model"]
        == "rasterio_numpy_candidate_extractor_v2"
    )
    assert result["summary"]["requested_targets"] == [
        "building",
        "road",
        "tree_canopy",
        "crop_patch",
        "linear_boundary",
    ]
    classes = {
        feature["properties"]["candidate_class"]
        for feature in result["geojson"]["features"]
    }
    assert {"building", "road", "tree_canopy", "crop_patch", "linear_boundary"} <= classes
