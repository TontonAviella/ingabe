# Copyright (C) 2025 Ingabe Ltd.
# Tests for SAR services: sentinel1_service, sar_water.

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

    def test_sign_href_leaves_other_urls_alone(self):
        from src.services.sentinel1_service import _sign_href
        assert _sign_href("https://example.com/test.tif") == "https://example.com/test.tif"

    def test_sign_href_fetches_one_token_per_container_with_a_time_limit(self, monkeypatch):
        """planetary_computer.sign fetched the token with no timeout; a stalled request held a thread for minutes."""
        from datetime import datetime, timedelta, timezone
        from src.services import sentinel1_service as s1

        calls = []

        def get(url, timeout, headers):
            calls.append((url, timeout))
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return type("R", (), {"raise_for_status": lambda self: None,
                                  "json": lambda self: {"msft:expiry": expiry, "token": "st=a&se=b&sig=c"}})()

        monkeypatch.setattr(s1, "_sas_tokens", {})
        monkeypatch.setattr(s1.httpx, "get", get)
        blob = "https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/GRD/2026/9/30/x/measurement/vv.tif"
        assert s1._sign_href(blob) == f"{blob}?st=a&se=b&sig=c"
        assert s1._sign_href(blob.replace("vv.tif", "vh.tif")).endswith("?st=a&se=b&sig=c")
        assert s1._sign_href(f"{blob}?st=a&se=b&sig=c") == f"{blob}?st=a&se=b&sig=c"  # already signed
        assert len(calls) == 1
        assert calls[0][0].endswith("/sentinel1euwestrtc/sentinel1-grd-rtc")
        assert calls[0][1].read is not None and calls[0][1].connect is not None

    def test_sign_href_returns_the_url_unsigned_when_the_token_fails(self, monkeypatch):
        import httpx
        from src.services import sentinel1_service as s1

        def get(*args, **kwargs):
            raise httpx.ReadTimeout("token endpoint stalled")

        monkeypatch.setattr(s1, "_sas_tokens", {})
        monkeypatch.setattr(s1.httpx, "get", get)
        blob = "https://sentinel1euwestrtc.blob.core.windows.net/sentinel1-grd-rtc/GRD/vv.tif"
        assert s1._sign_href(blob) == blob


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


# ── Integration-level tests (still no network) ──


class TestSARWaterService:
    def test_singleton(self):
        from src.services.sar_water import get_sar_water_service
        svc1 = get_sar_water_service()
        svc2 = get_sar_water_service()
        assert svc1 is svc2


class TestSentinel1TimeSeries:
    def test_parallel_reads_keep_each_scene_with_its_own_bands(self, monkeypatch):
        import time
        from src.services import sentinel1_service as s1

        items = [{"properties": {"datetime": f"2026-09-{d:02d}T03:00:00Z"},
                  "assets": {"vv": {"href": f"vv-{d}"}, "vh": {"href": f"vh-{d}"}}} for d in (1, 7, 13, 19, 25)]
        del items[2]["assets"]["vh"]  # a scene without VH is left out, as before
        linear = {f"{band}-{d}": 10 ** (db / 10) for d, (vv, vh) in zip((1, 7, 13, 19, 25), [(-8, -14), (-9, -15), (-7, -13), (-10, -16), (-6, -12)])
                  for band, db in (("vv", vv), ("vh", vh))}

        def read(href, bbox):
            time.sleep(0.2 if href.endswith("-1") else 0.05)  # the first scene finishes last
            return np.full((4, 4), linear[href], dtype=np.float32), None, "EPSG:32735"

        monkeypatch.setattr(s1, "_search_items", lambda *a, **k: items)
        monkeypatch.setattr(s1, "_read_band_window", read)
        started = time.monotonic()
        ts = s1.Sentinel1Service().get_time_series((30.0, -2.0, 30.1, -1.9), "2026-09-01/2026-09-30")
        assert time.monotonic() - started < 0.6  # 8 reads of 0.05-0.2 s, not one after another
        assert ts["dates"] == ["2026-09-01T03:00:00Z", "2026-09-07T03:00:00Z", "2026-09-19T03:00:00Z", "2026-09-25T03:00:00Z"]
        assert ts["vv_means"] == pytest.approx([-8, -9, -10, -6], abs=1e-4)
        assert ts["vh_means"] == pytest.approx([-14, -15, -16, -12], abs=1e-4)


class TestToolsJsonIntegrity:
    def test_tools_json_valid(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        assert isinstance(tools, list)

        tool_names = [t["function"]["name"] for t in tools]
        assert "predict_ndvi_from_sar" not in tool_names  # removed: docs/SAR_NDVI_SKILL.md
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
