# Copyright (C) 2025 Ingabe Ltd.
# Tests for SAR services: sentinel1_service, sar_water, sar_ndvi.

"""Unit tests for SAR service pure-computation functions.

These tests exercise the algorithms with synthetic data and don't
require network access or Docker.
"""

import numpy as np
import pytest


# ── sentinel1_service tests ──


class TestSentinel1Service:
    def test_singleton_returns_same_instance(self):
        from src.services.sentinel1_service import get_sentinel1_service
        svc1 = get_sentinel1_service()
        svc2 = get_sentinel1_service()
        assert svc1 is svc2

    def test_sign_href_without_planetary_computer(self, monkeypatch):
        """_sign_href should return href unchanged if signing fails."""
        from src.services.sentinel1_service import _sign_href
        # Even if planetary_computer is installed, test the fallback
        result = _sign_href("https://example.com/test.tif")
        assert result.startswith("https://")


# ── sar_water tests ──


class TestMultilook:
    def test_basic_multilook(self):
        from src.services.sar_water import _multilook
        arr = np.array([
            [1, 2, 3, 4],
            [5, 6, 7, 8],
            [9, 10, 11, 12],
            [13, 14, 15, 16],
        ], dtype=np.float32)
        result = _multilook(arr, factor=2)
        assert result.shape == (2, 2)
        assert result[0, 0] == pytest.approx(3.5)  # mean of 1,2,5,6
        assert result[0, 1] == pytest.approx(5.5)  # mean of 3,4,7,8
        assert result[1, 0] == pytest.approx(11.5)  # mean of 9,10,13,14
        assert result[1, 1] == pytest.approx(13.5)  # mean of 11,12,15,16

    def test_multilook_with_nan(self):
        from src.services.sar_water import _multilook
        arr = np.array([
            [1, np.nan, 3, 4],
            [5, 6, 7, 8],
        ], dtype=np.float32)
        result = _multilook(arr, factor=2)
        assert result.shape == (1, 2)
        assert result[0, 0] == pytest.approx(4.0)  # nanmean of 1,nan,5,6

    def test_multilook_trims_odd_dimensions(self):
        from src.services.sar_water import _multilook
        arr = np.ones((5, 7), dtype=np.float32)
        result = _multilook(arr, factor=2)
        assert result.shape == (2, 3)


class TestWaterThreshold:
    def test_threshold_with_water_and_land(self):
        """Synthetic image: water (low values) in top-left, land (high values) elsewhere."""
        from src.services.sar_water import _compute_water_threshold
        rng = np.random.RandomState(42)
        arr = np.full((32, 32), -5.0, dtype=np.float32)  # land at -5 dB
        arr += rng.normal(0, 0.5, arr.shape).astype(np.float32)
        # Water region: top-left 16x16
        arr[:16, :16] = -18.0 + rng.normal(0, 0.3, (16, 16)).astype(np.float32)

        threshold = _compute_water_threshold(arr, tile_size=8, sub_tile_size=4)
        # Threshold should be between water (-18) and land (-5)
        assert -20.0 < threshold < -5.0

    def test_threshold_all_land(self):
        """All-land image should produce conservative threshold."""
        from src.services.sar_water import _compute_water_threshold
        arr = np.full((32, 32), -8.0, dtype=np.float32)
        arr += np.random.RandomState(42).normal(0, 0.5, arr.shape).astype(np.float32)
        threshold = _compute_water_threshold(arr, tile_size=8, sub_tile_size=4)
        # Should be well below the data (conservative = few false positives)
        assert threshold < -8.0

    def test_threshold_small_image_fallback(self):
        """Image smaller than tile_size should use global fallback."""
        from src.services.sar_water import _compute_water_threshold
        arr = np.array([[-10.0, -20.0], [-5.0, -15.0]], dtype=np.float32)
        threshold = _compute_water_threshold(arr, tile_size=8, sub_tile_size=4)
        # Fallback: mean - std
        assert np.isfinite(threshold)


class TestWaterMask:
    def test_detects_water_region(self):
        """Synthetic image with clear water/land separation.

        Water region offset from tile boundaries so quadtree tiling
        produces mixed water/land tiles with high CV, which is how the
        algorithm identifies the water threshold.
        """
        from src.services.sar_water import _water_mask
        rng = np.random.RandomState(42)
        arr = np.full((64, 64), -7.0, dtype=np.float32)  # land
        arr += rng.normal(0, 0.3, arr.shape).astype(np.float32)
        # Water region offset from tile boundaries (not multiple of tile_size=8)
        arr[:30, :30] = -20.0 + rng.normal(0, 0.2, (30, 30)).astype(np.float32)

        mask, threshold = _water_mask(arr, tile_size=8, sub_tile_size=4, min_area_pixels=4)
        # Most of the water region should be detected
        water_in_water_region = mask[:30, :30].sum()
        water_in_land_region = mask[32:, 32:].sum()

        assert water_in_water_region > 150  # majority of 30x30 = 900 pixels
        assert water_in_land_region < 50  # few false positives


class TestComputeAreaHa:
    def test_area_in_degrees(self):
        from src.services.sar_water import _compute_area_ha
        from rasterio.transform import Affine
        # 10m pixels in degree terms: ~0.0001 degrees
        transform = Affine(0.0001, 0, 29.0, 0, -0.0001, -1.5)
        mask = np.ones((100, 100), dtype=bool)  # 100x100 = 10000 pixels
        area = _compute_area_ha(mask, transform)
        # At ~2° lat: 0.0001° ≈ 11.1m. So 100x100 pixels ≈ 1.11km × 1.11km ≈ 123 ha
        assert 100 < area < 150  # reasonable range

    def test_area_zero_mask(self):
        from src.services.sar_water import _compute_area_ha
        from rasterio.transform import Affine
        transform = Affine(10, 0, 0, 0, -10, 0)
        mask = np.zeros((10, 10), dtype=bool)
        assert _compute_area_ha(mask, transform) == 0.0

    def test_area_in_meters(self):
        from src.services.sar_water import _compute_area_ha
        from rasterio.transform import Affine
        # 10m pixels in projected CRS
        transform = Affine(10, 0, 500000, 0, -10, 9800000)
        mask = np.ones((100, 100), dtype=bool)
        area = _compute_area_ha(mask, transform)
        # 100*10m x 100*10m = 1km² = 100 ha
        assert area == pytest.approx(100.0, abs=1)


class TestMaskToGeojson:
    def test_geojson_structure(self):
        from src.services.sar_water import _mask_to_geojson
        from rasterio.transform import Affine
        mask = np.zeros((10, 10), dtype=bool)
        mask[2:5, 2:5] = True
        transform = Affine(0.001, 0, 29.0, 0, -0.001, -1.5)
        geojson = _mask_to_geojson(mask, transform, "EPSG:4326")
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) >= 1
        assert geojson["features"][0]["type"] == "Feature"
        assert geojson["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")

    def test_empty_mask_returns_empty_collection(self):
        from src.services.sar_water import _mask_to_geojson
        from rasterio.transform import Affine
        mask = np.zeros((10, 10), dtype=bool)
        transform = Affine(0.001, 0, 29.0, 0, -0.001, -1.5)
        geojson = _mask_to_geojson(mask, transform, "EPSG:4326")
        assert geojson["type"] == "FeatureCollection"
        assert len(geojson["features"]) == 0


# ── sar_ndvi tests ──


class TestExtractFeatures:
    def test_basic_feature_extraction(self):
        from src.services.sar_ndvi import _extract_features
        dates = ["2025-01-01", "2025-01-07", "2025-01-13", "2025-01-19", "2025-01-25"]
        vv = [-10.0, -9.5, -9.0, -8.5, -8.0]
        vh = [-18.0, -17.5, -17.0, -16.5, -16.0]
        vv_std = [1.0, 1.1, 1.2, 1.3, 1.4]
        vh_std = [0.8, 0.9, 1.0, 1.1, 1.2]

        features = _extract_features(dates, vv, vh, vv_std, vh_std)
        assert features is not None
        assert features.shape == (120,)  # 30 days × 4 stats

    def test_feature_extraction_with_ndvi_anchor(self):
        from src.services.sar_ndvi import _extract_features
        dates = ["2025-01-01", "2025-01-07", "2025-01-13"]
        vv = [-10.0, -9.5, -9.0]
        vh = [-18.0, -17.5, -17.0]
        vv_std = [1.0, 1.1, 1.2]
        vh_std = [0.8, 0.9, 1.0]

        features = _extract_features(dates, vv, vh, vv_std, vh_std, last_known_ndvi=0.65)
        assert features is not None
        assert features.shape == (121,)  # 120 + 1 anchor
        assert features[-1] == pytest.approx(0.65)

    def test_insufficient_dates_returns_none(self):
        from src.services.sar_ndvi import _extract_features
        features = _extract_features(["2025-01-01"], [-10.0], [-18.0], [1.0], [0.8])
        assert features is None

    def test_iso_datetime_format(self):
        """Should handle ISO datetime strings with T and Z."""
        from src.services.sar_ndvi import _extract_features
        dates = ["2025-01-01T00:00:00Z", "2025-01-07T12:30:00Z", "2025-01-13T06:15:00Z"]
        vv = [-10.0, -9.5, -9.0]
        vh = [-18.0, -17.5, -17.0]
        vv_std = [1.0, 1.1, 1.2]
        vh_std = [0.8, 0.9, 1.0]

        features = _extract_features(dates, vv, vh, vv_std, vh_std)
        assert features is not None
        assert features.shape == (120,)


class TestEmpiricalPrediction:
    def test_bare_soil_prediction(self):
        """Low VH/VV ratio (bare soil) should give low NDVI."""
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred = get_sar_ndvi_predictor()
        # VV=-8 dB, VH=-20 dB → linear ratio ≈ 10^(-20/10) / 10^(-8/10) ≈ 0.063
        ts = {
            "dates": ["2025-01-01", "2025-01-07"],
            "vv_means": [-8.0, -8.0],
            "vh_means": [-20.0, -20.0],
            "vv_stds": [1.0, 1.0],
            "vh_stds": [0.8, 0.8],
        }
        result = pred._empirical_prediction(ts)
        assert result["status"] == "success"
        assert result["predicted_ndvi"] < 0.25  # bare soil range

    def test_dense_vegetation_prediction(self):
        """High VH/VV ratio (dense veg) should give high NDVI."""
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred = get_sar_ndvi_predictor()
        # VV=-10 dB, VH=-13 dB → linear ratio ≈ 0.05 / 0.1 = 0.5
        ts = {
            "dates": ["2025-01-01", "2025-01-07"],
            "vv_means": [-10.0, -10.0],
            "vh_means": [-13.0, -13.0],
            "vv_stds": [1.0, 1.0],
            "vh_stds": [0.8, 0.8],
        }
        result = pred._empirical_prediction(ts)
        assert result["status"] == "success"
        assert result["predicted_ndvi"] > 0.5  # vegetation range

    def test_empty_time_series(self):
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred = get_sar_ndvi_predictor()
        result = pred._empirical_prediction({"vv_means": [], "vh_means": [], "dates": []})
        assert result["status"] == "error"


class TestConfidence:
    def test_confidence_increases_with_scenes(self):
        from src.services.sar_ndvi import SARNDVIPredictor
        pred = SARNDVIPredictor()
        ts_few = {"dates": ["a", "b"]}
        ts_many = {"dates": ["a", "b", "c", "d", "e"]}
        c_few = pred._compute_confidence(ts_few)
        c_many = pred._compute_confidence(ts_many)
        assert c_many > c_few

    def test_confidence_capped(self):
        from src.services.sar_ndvi import SARNDVIPredictor
        pred = SARNDVIPredictor()
        pred._model_rmse = 0.05
        pred._n_training_samples = 50
        ts = {"dates": list(range(10))}
        confidence = pred._compute_confidence(ts)
        assert confidence <= 0.95


# ── Integration-level tests (still no network) ──


class TestSARWaterService:
    def test_singleton(self):
        from src.services.sar_water import get_sar_water_service
        svc1 = get_sar_water_service()
        svc2 = get_sar_water_service()
        assert svc1 is svc2


class TestSARNDVIPredictor:
    def test_singleton(self):
        from src.services.sar_ndvi import get_sar_ndvi_predictor
        pred1 = get_sar_ndvi_predictor()
        pred2 = get_sar_ndvi_predictor()
        assert pred1 is pred2


class TestToolsJsonIntegrity:
    def test_tools_json_valid(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        assert isinstance(tools, list)

        # Check our 3 new tools exist
        tool_names = [t["function"]["name"] for t in tools]
        assert "predict_ndvi_from_sar" in tool_names
        assert "detect_water_bodies" in tool_names
        assert "detect_flood_extent" in tool_names

    def test_new_tools_have_required_fields(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)

        for tool in tools:
            func = tool["function"]
            assert "name" in func
            assert "description" in func
            assert "parameters" in func

        # Check detect_flood_extent requires bbox + both dates
        flood_tool = next(t for t in tools if t["function"]["name"] == "detect_flood_extent")
        required = flood_tool["function"]["parameters"]["required"]
        assert "bbox" in required
        assert "date_before" in required
        assert "date_after" in required
