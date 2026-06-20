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
    assert result["summary"]["class_counts"]["building"] >= 3
    assert result["summary"]["analytics_format"] == "geoparquet"
    assert result["summary"]["geojson_role"] == "live_map_transport_only"
    assert result["summary"]["confirmed_count"] is False
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
