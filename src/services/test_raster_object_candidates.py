from __future__ import annotations

import os

import geopandas as gpd
import numpy as np
import rasterio
from affine import Affine
from rasterio.transform import from_origin

import src.services.raster_object_candidates as raster_object_candidates
from src.services.raster_object_candidates import (
    RasterObjectCandidateInput,
    analyze_raster_object_candidates,
)


def test_building_threshold_applies_to_reported_confidence(monkeypatch) -> None:
    mask = np.zeros((20, 20), dtype=bool)
    mask[4:16, 4:16] = True
    monkeypatch.setattr(raster_object_candidates, "_confidence", lambda *_args: 0.6504)

    features = raster_object_candidates._features_from_mask(
        mask,
        target="building",
        source_transform=from_origin(0, 20, 1, 1),
        source_crs="EPSG:3857",
        min_area_m2=8,
        max_area_m2=500,
        confidence_threshold=0.65,
        max_candidates=10,
    )

    assert features == []


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


def test_analyze_raster_object_candidates_can_screen_road_like_segments(
    tmp_path,
) -> None:
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
        result["summary"]["screening_model"] == "rasterio_numpy_candidate_extractor_v2"
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
    assert {
        "building",
        "road",
        "tree_canopy",
        "crop_patch",
        "linear_boundary",
    } <= classes


def test_analyze_raster_object_candidates_uses_fastsam_masks_when_requested(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "synthetic_fastsam_ortho.tif"
    image = np.zeros((3, 120, 120), dtype=np.uint8)
    image[0, :, :] = 55
    image[1, :, :] = 145
    image[2, :, :] = 65
    image[:, 24:42, 24:48] = np.array([235, 232, 220], dtype=np.uint8)[:, None, None]

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

    class _FakeMaskTensor:
        def __init__(self, data: np.ndarray) -> None:
            self._data = data

        def detach(self) -> "_FakeMaskTensor":
            return self

        def cpu(self) -> "_FakeMaskTensor":
            return self

        def numpy(self) -> np.ndarray:
            return self._data

    class _FakeMasks:
        def __init__(self, data: np.ndarray) -> None:
            self.data = _FakeMaskTensor(data)

    class _FakeResult:
        def __init__(self, data: np.ndarray) -> None:
            self.masks = _FakeMasks(data)

    class _FakeFastSAM:
        def __call__(self, image_array, **kwargs):
            mask = np.zeros(image_array.shape[:2], dtype="float32")
            mask[24:42, 24:48] = 1.0
            return [_FakeResult(mask[None, :, :])]

    monkeypatch.setattr(
        raster_object_candidates,
        "_fastsam_weights_status",
        lambda: {"available": True, "path": "/tmp/FastSAM-s.pt", "size_bytes": 1},
    )
    monkeypatch.setattr(
        raster_object_candidates,
        "_load_fastsam_model",
        lambda _weights_path: _FakeFastSAM(),
    )

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lfastsam",
            layer_name="Synthetic FastSAM Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=10,
            max_sample_pixels=20_000,
            min_area_m2=20,
            max_area_m2=1000,
            confidence_threshold=0.65,
            engine_preference="fastsam",
        )
    )

    assert result["status"] == "success"
    assert result["engines"]["selection"]["used"] == "fastsam_s_candidate_masks_v1"
    assert result["summary"]["screening_model"] == "fastsam_s_candidate_masks_v1"
    assert result["summary"]["fastsam_status"]["status"] == "success"
    assert result["summary"]["class_counts"]["building"] >= 1
    assert {
        feature["properties"]["screening_model"]
        for feature in result["geojson"]["features"]
    } == {"fastsam_s_candidate_masks_v1"}
    assert {
        feature["properties"]["fastsam_geometry_source"]
        for feature in result["geojson"]["features"]
    } == {"fastsam_object_mask"}
    assert all(
        feature["properties"]["fastsam_object_overlap"] >= 0.24
        for feature in result["geojson"]["features"]
    )


def test_analyze_raster_object_candidates_adds_small_roof_recall_after_fastsam(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "synthetic_fastsam_roof_recall.tif"
    image = np.zeros((3, 120, 120), dtype=np.uint8)
    image[0, :, :] = 55
    image[1, :, :] = 145
    image[2, :, :] = 65
    image[:, 24:42, 24:48] = np.array([235, 232, 220], dtype=np.uint8)[:, None, None]
    image[:, 58:74, 60:78] = np.array([238, 235, 224], dtype=np.uint8)[:, None, None]

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

    class _FakeMaskTensor:
        def __init__(self, data: np.ndarray) -> None:
            self._data = data

        def detach(self) -> "_FakeMaskTensor":
            return self

        def cpu(self) -> "_FakeMaskTensor":
            return self

        def numpy(self) -> np.ndarray:
            return self._data

    class _FakeMasks:
        def __init__(self, data: np.ndarray) -> None:
            self.data = _FakeMaskTensor(data)

    class _FakeResult:
        def __init__(self, data: np.ndarray) -> None:
            self.masks = _FakeMasks(data)

    class _FakeFastSAM:
        def __call__(self, image_array, **kwargs):
            mask = np.zeros(image_array.shape[:2], dtype="float32")
            mask[24:42, 24:48] = 1.0
            return [_FakeResult(mask[None, :, :])]

    monkeypatch.setattr(
        raster_object_candidates,
        "_fastsam_weights_status",
        lambda: {"available": True, "path": "/tmp/FastSAM-s.pt", "size_bytes": 1},
    )
    monkeypatch.setattr(
        raster_object_candidates,
        "_load_fastsam_model",
        lambda _weights_path: _FakeFastSAM(),
    )

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lfastsamrecall",
            layer_name="Synthetic FastSAM Roof Recall Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=10,
            max_sample_pixels=20_000,
            min_area_m2=20,
            max_area_m2=1000,
            confidence_threshold=0.65,
            engine_preference="fastsam",
        )
    )

    assert result["status"] == "success"
    sources = {
        feature["properties"].get("fastsam_geometry_source")
        for feature in result["geojson"]["features"]
    }
    assert "fastsam_object_mask" in sources
    assert "roof_evidence_component_after_fastsam" in sources
    assert result["summary"]["class_counts"]["building"] >= 2


def test_analyze_raster_object_candidates_requires_fastsam_when_requested(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "synthetic_missing_fastsam_ortho.tif"
    image = np.zeros((3, 80, 80), dtype=np.uint8)
    image[0, :, :] = 55
    image[1, :, :] = 145
    image[2, :, :] = 65
    image[:, 20:36, 20:40] = np.array([235, 232, 220], dtype=np.uint8)[:, None, None]

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=80,
        height=80,
        count=3,
        dtype="uint8",
        crs="EPSG:3857",
        transform=from_origin(0, 80, 1, 1),
    ) as ds:
        ds.write(image)

    monkeypatch.setattr(
        raster_object_candidates,
        "_fastsam_weights_status",
        lambda: {
            "available": False,
            "reason": "FastSAM-s.pt not found in test",
        },
    )

    result = analyze_raster_object_candidates(
        RasterObjectCandidateInput(
            raster_url=str(path),
            layer_id="Lmissingfastsam",
            layer_name="Missing FastSAM Orthophoto",
            bounds_wgs84=None,
            target_classes=["building"],
            max_candidates=10,
            max_sample_pixels=20_000,
            min_area_m2=8,
            max_area_m2=600,
            confidence_threshold=0.20,
            engine_preference="fastsam",
        )
    )

    assert result["status"] == "error"
    assert "FastSAM is required" in result["error"]
    assert result["engines"]["selection"]["used"] == "fastsam_required_unavailable"
    assert result["summary"]["candidate_count"] == 0
    assert result["summary"]["count_semantics"] == "not_available_fastsam_required"


def test_fastsam_tiles_keep_masks_local_and_accumulate_one_coverage_mask(
    monkeypatch,
) -> None:
    height = width = 500
    tile_size = 256
    stride = 192
    rgb = np.full((height, width, 3), 100, dtype=np.uint8)
    building_mask = np.ones((height, width), dtype=bool)
    source_transform = from_origin(100, 1_000, 2, 3)
    extractor_calls: list[dict[str, object]] = []
    supplemental_calls: list[np.ndarray] = []

    class _FakeMasks:
        def __init__(self, data: np.ndarray) -> None:
            self.data = data

    class _FakeResult:
        def __init__(self, data: np.ndarray) -> None:
            self.masks = _FakeMasks(data)

    class _FakeFastSAM:
        def __call__(self, tile, **_kwargs):
            return [_FakeResult(np.ones((1, *tile.shape[:2]), dtype=np.float32))]

    def _fake_object_features(object_mask, **kwargs):
        transform = kwargs["source_transform"]
        extractor_calls.append(
            {
                "object_shape": object_mask.shape,
                "target_shape": kwargs["target_masks"]["building"].shape,
                "origin": transform * (0, 0),
                "target_pixels": kwargs["target_pixel_counts"]["building"],
                "image_pixels": kwargs["image_pixels"],
            }
        )
        origin_x, origin_y = transform * (0, 0)
        return [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [origin_x, origin_y],
                            [origin_x + 1, origin_y],
                            [origin_x + 1, origin_y + 1],
                            [origin_x, origin_y + 1],
                            [origin_x, origin_y],
                        ]
                    ],
                },
                "properties": {
                    "candidate_class": "building",
                    "confidence": 0.9,
                },
            }
        ]

    def _fake_supplemental(*, accepted_coverage_mask, **_kwargs):
        supplemental_calls.append(accepted_coverage_mask)
        return []

    monkeypatch.setenv("MUNDI_FASTSAM_TILE_SIZE", str(tile_size))
    monkeypatch.setenv("MUNDI_FASTSAM_TILE_STRIDE", str(stride))
    monkeypatch.setattr(
        raster_object_candidates,
        "_features_from_fastsam_object_mask",
        _fake_object_features,
    )
    monkeypatch.setattr(
        raster_object_candidates,
        "_fastsam_supplemental_roof_evidence_features",
        _fake_supplemental,
    )

    features, mask_count = raster_object_candidates._features_from_fastsam_tiles(
        _FakeFastSAM(),
        rgb,
        target_masks={"building": building_mask},
        targets=["building"],
        source_transform=source_transform,
        source_crs="EPSG:3857",
        min_area_m2=1,
        max_area_m2=1_000,
        confidence_threshold=0.2,
        max_candidates=100,
    )

    tile_starts = raster_object_candidates._tile_starts(width, tile_size, stride)
    expected_origins = [
        (source_transform * Affine.translation(x0, y0)) * (0, 0)
        for y0 in tile_starts
        for x0 in tile_starts
    ]
    assert mask_count == len(expected_origins)
    assert len(features) == len(expected_origins)
    assert [call["origin"] for call in extractor_calls] == expected_origins
    assert {call["object_shape"] for call in extractor_calls} == {
        (tile_size, tile_size)
    }
    assert {call["target_shape"] for call in extractor_calls} == {
        (tile_size, tile_size)
    }
    assert {call["target_pixels"] for call in extractor_calls} == {height * width}
    assert {call["image_pixels"] for call in extractor_calls} == {height * width}
    assert len(supplemental_calls) == 1
    coverage_mask = supplemental_calls[0]
    assert isinstance(coverage_mask, np.ndarray)
    assert coverage_mask.shape == (height, width)
    assert coverage_mask.dtype == np.bool_
    assert np.all(coverage_mask)


def _candidate_feature(
    name: str,
    bounds: tuple[float, float, float, float],
    confidence: float,
    candidate_class: str = "building",
) -> dict[str, object]:
    minx, miny, maxx, maxy = bounds
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [
                [
                    [minx, miny],
                    [maxx, miny],
                    [maxx, maxy],
                    [minx, maxy],
                    [minx, miny],
                ]
            ],
        },
        "properties": {
            "name": name,
            "candidate_class": candidate_class,
            "confidence": confidence,
        },
    }


def test_dedupe_candidate_features_preserves_confidence_first_iou_semantics() -> None:
    features = [
        _candidate_feature("low_exact", (0, 0, 10, 10), 0.1),
        _candidate_feature("survives_chain", (3.2, 0, 13.2, 10), 0.7),
        _candidate_feature("suppressed", (1.6, 0, 11.6, 10), 0.8),
        _candidate_feature("winner", (0, 0, 10, 10), 0.9),
        _candidate_feature("other_class", (0, 0, 10, 10), 0.6, "road"),
        _candidate_feature("below_threshold", (21.7, 0, 31.7, 10), 0.4),
        _candidate_feature("separate_winner", (20, 0, 30, 10), 0.5),
        _candidate_feature("merged_level_duplicate", (23.2, 0, 33.2, 10), 0.3),
    ]

    deduped = raster_object_candidates._dedupe_candidate_features(features)

    assert [feature["properties"]["name"] for feature in deduped] == [
        "winner",
        "survives_chain",
        "other_class",
        "separate_winner",
        "below_threshold",
    ]


def test_dedupe_candidate_features_uses_bounded_spatial_queries(monkeypatch) -> None:
    sparse_count = 600
    dense_count = 600
    features = [
        _candidate_feature(
            f"sparse_{index}",
            (index * 20.0, 20, index * 20.0 + 10, 30),
            0.8,
        )
        for index in range(sparse_count)
    ]
    features.extend(
        _candidate_feature(
            f"dense_{index}",
            (index * 0.00001, 0, 10 + index * 0.00001, 10),
            0.7,
        )
        for index in range(dense_count)
    )
    original_shape = raster_object_candidates.shape
    original_bbox_iou = raster_object_candidates._bbox_iou
    shape_calls = 0
    bbox_iou_calls = 0

    def _counting_shape(geometry):
        nonlocal shape_calls
        shape_calls += 1
        return original_shape(geometry)

    def _counting_bbox_iou(a, b):
        nonlocal bbox_iou_calls
        bbox_iou_calls += 1
        return original_bbox_iou(a, b)

    monkeypatch.setattr(raster_object_candidates, "shape", _counting_shape)
    monkeypatch.setattr(raster_object_candidates, "_bbox_iou", _counting_bbox_iou)

    deduped = raster_object_candidates._dedupe_candidate_features(features)

    assert len(deduped) == sparse_count + 1
    assert shape_calls == len(features)
    assert bbox_iou_calls < len(features) * 2


def test_building_confidence_boundary_is_enforced_at_point_65(monkeypatch) -> None:
    mask = np.zeros((20, 20), dtype=bool)
    mask[4:12, 5:13] = True

    for confidence, expected_count in ((0.649, 0), (0.65, 0), (0.651, 1)):
        monkeypatch.setattr(
            raster_object_candidates,
            "_confidence",
            lambda *_args, value=confidence, **_kwargs: value,
        )
        features = raster_object_candidates._features_from_mask(
            mask,
            target="building",
            source_transform=from_origin(0, 20, 1, 1),
            source_crs="EPSG:3857",
            min_area_m2=1,
            max_area_m2=1_000,
            confidence_threshold=0.1,
            max_candidates=10,
            screening_model="fastsam_s_candidate_masks_v1",
        )

        assert len(features) == expected_count
        if features:
            assert features[0]["properties"]["confidence_threshold_used"] == 0.65
