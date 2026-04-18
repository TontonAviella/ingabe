# Copyright (C) 2025 Ingabe Ltd.
# Tests for ALOS PALSAR + CYGNSS services and their Sage tool wiring.

"""Unit tests for alos_palsar + cygnss services.

Covers pure-computation helpers, singletons, graceful degradation
when auth is absent, and tools.json integrity for the 5 new tools.
"""

from __future__ import annotations

import numpy as np
import pytest


# ── alos_palsar helpers ──


class TestSafeRound:
    def test_finite_value_rounds(self):
        from src.services.alos_palsar import _safe_round
        assert _safe_round(1.23456789) == 1.2346

    def test_nan_returns_zero(self):
        from src.services.alos_palsar import _safe_round
        assert _safe_round(float("nan")) == 0.0

    def test_inf_returns_zero(self):
        from src.services.alos_palsar import _safe_round
        assert _safe_round(float("inf")) == 0.0


class TestBandStats:
    def test_empty_valid_returns_zeros(self):
        from src.services.alos_palsar import _band_stats
        arr = np.array([1.0, 2.0, 3.0])
        valid = np.array([False, False, False])
        out = _band_stats(arr, valid)
        assert out["mean"] == 0.0
        assert out["valid_pixels"] == 0
        assert out["no_data_pixels"] == 3

    def test_basic_stats(self):
        from src.services.alos_palsar import _band_stats
        arr = np.array([-10.0, -8.0, -6.0, -4.0])
        valid = np.ones_like(arr, dtype=bool)
        out = _band_stats(arr, valid)
        assert out["mean"] == pytest.approx(-7.0, abs=1e-3)
        assert out["min"] == -10.0
        assert out["max"] == -4.0
        assert out["valid_pixels"] == 4
        assert "percentiles" in out


class TestGammaNaughtConversion:
    def test_dn_to_db_basic(self):
        from src.services.alos_palsar import _to_gamma_naught_db
        # DN=1000 → gamma0 = 10*log10(1e6) - 83 = 60 - 83 = -23 dB
        dn = np.array([[1000, 2000], [500, 0]], dtype=np.uint16)
        out = _to_gamma_naught_db(dn)
        assert out[0, 0] == pytest.approx(-23.0, abs=0.01)
        # DN=0 is nodata → NaN
        assert np.isnan(out[1, 1])

    def test_preserves_float32_dtype(self):
        from src.services.alos_palsar import _to_gamma_naught_db
        dn = np.array([[100, 200]], dtype=np.uint16)
        out = _to_gamma_naught_db(dn)
        assert out.dtype == np.float32


class TestALOSPALSARService:
    def test_singleton(self):
        from src.services.alos_palsar import get_alos_palsar_service, ALOSPALSARService
        s1 = get_alos_palsar_service()
        s2 = get_alos_palsar_service()
        assert s1 is s2
        assert isinstance(s1, ALOSPALSARService)

    def test_rwanda_bbox_constant(self):
        from src.services.alos_palsar import RWANDA_BBOX
        assert RWANDA_BBOX == (28.86, -2.84, 30.90, -1.05)

    def test_temporal_variation_insufficient_years(self, monkeypatch):
        """When only one year returns success, we report insufficient_years."""
        from src.services import alos_palsar
        svc = alos_palsar.ALOSPALSARService()

        def fake_stats(bbox, years=None):
            return {
                "source": "deafrica_stac",
                "collection": "alos_palsar_mosaic",
                "sensor": "ALOS-2/PALSAR-2",
                "band": "L-band",
                "resolution_m": 25,
                "bbox": list(bbox),
                "years": [
                    {"year": 2020, "status": "success",
                     "hh_db": {"mean": -12.0}, "hv_db": {"mean": -18.0},
                     "hh_hv_ratio_db": {"mean": 6.0, "std": 0.5}},
                ],
            }

        monkeypatch.setattr(svc, "get_l_band_stats", fake_stats)
        out = svc.get_temporal_variation(bbox=(29, -2, 30, -1), years=[2020, 2021])
        assert out["status"] == "insufficient_years"

    def test_temporal_variation_success(self, monkeypatch):
        from src.services import alos_palsar
        svc = alos_palsar.ALOSPALSARService()

        def fake_stats(bbox, years=None):
            return {
                "years": [
                    {"year": 2020, "status": "success",
                     "hh_db": {"mean": -10.0}, "hv_db": {"mean": -15.0},
                     "hh_hv_ratio_db": {"mean": 5.0, "std": 0.3}},
                    {"year": 2021, "status": "success",
                     "hh_db": {"mean": -11.0}, "hv_db": {"mean": -17.0},
                     "hh_hv_ratio_db": {"mean": 6.0, "std": 0.4}},
                    {"year": 2022, "status": "success",
                     "hh_db": {"mean": -12.0}, "hv_db": {"mean": -19.0},
                     "hh_hv_ratio_db": {"mean": 7.0, "std": 0.5}},
                ],
            }

        monkeypatch.setattr(svc, "get_l_band_stats", fake_stats)
        out = svc.get_temporal_variation(bbox=(29, -2, 30, -1), years=[2020, 2021, 2022])
        assert len(out["yearly_stats"]) == 3
        assert out["inter_annual_variation"]["ratio_range_db"] == 2.0  # 7 - 5
        assert out["inter_annual_variation"]["ratio_mean_across_years"] == 6.0


# ── cygnss tests ──


class TestCYGNSSService:
    def test_singleton(self):
        from src.services.cygnss import get_cygnss_service, CYGNSSService
        s1 = get_cygnss_service()
        s2 = get_cygnss_service()
        assert s1 is s2
        assert isinstance(s1, CYGNSSService)

    def test_rwanda_bbox_constant(self):
        from src.services.cygnss import RWANDA_BBOX
        assert RWANDA_BBOX == (28.86, -2.84, 30.90, -1.05)

    def test_search_granules_unknown_product(self):
        from src.services.cygnss import CYGNSSService
        svc = CYGNSSService()
        out = svc.search_granules(product="not_a_real_product")
        assert out["status"] == "error"
        assert "Unknown product" in out["message"]

    def test_search_granules_mocked_success(self, monkeypatch):
        """Mock CMR response, verify parsing."""
        from src.services import cygnss

        class FakeResp:
            status_code = 200
            def raise_for_status(self): pass
            def json(self):
                return {
                    "feed": {"entry": [
                        {
                            "title": "CYGNSS_L3_SOIL_MOISTURE_9km_2026-03-15",
                            "time_start": "2026-03-15T00:00:00Z",
                            "time_end": "2026-03-15T23:59:59Z",
                            "links": [
                                {"href": "https://archive.podaac.earthdata.nasa.gov/foo.nc"},
                                {"href": "https://opendap.earthdata.nasa.gov/collections/bar"},
                            ],
                        },
                    ]},
                }

        monkeypatch.setattr(cygnss.httpx, "get", lambda *a, **kw: FakeResp())
        svc = cygnss.CYGNSSService()
        out = svc.search_granules(product="soil_moisture_9km", days_back=7)
        assert out["status"] == "success"
        assert out["granules_found"] == 1
        g = out["granules"][0]
        assert g["data_url"].endswith(".nc")
        assert g["opendap_url"] is not None

    def test_soil_moisture_auth_required_without_creds(self, monkeypatch):
        """Without Earthdata creds, we return auth_required gracefully."""
        from src.services import cygnss

        svc = cygnss.CYGNSSService()
        # Force no creds
        monkeypatch.setattr(svc, "_get_earthdata_auth", lambda: None)
        monkeypatch.setattr(svc, "_get_earthaccess_session", lambda: None)
        # Mock search to return a granule
        monkeypatch.setattr(svc, "search_granules", lambda **kw: {
            "status": "success",
            "granules": [{"time_start": "2026-03-15T00:00:00Z"}],
            "granules_found": 1,
            "date_range": {"start": "2026-01-15", "end": "2026-04-15"},
        })
        out = svc.get_soil_moisture(lat=-1.95, lon=29.87, days_back=90)
        assert out["status"] == "auth_required"
        assert out["lat"] == -1.95
        assert out["lon"] == 29.87
        assert "EARTHDATA" in out["message"]

    def test_watermask_auth_required_without_creds(self, monkeypatch):
        from src.services import cygnss
        svc = cygnss.CYGNSSService()
        monkeypatch.setattr(svc, "_get_earthaccess_session", lambda: None)
        monkeypatch.setattr(svc, "search_granules", lambda **kw: {
            "status": "success",
            "granules": [{"time_start": "2026-03-15T00:00:00Z"}],
            "granules_found": 1,
        })
        out = svc.get_watermask(bbox=(29, -2, 30, -1), date="2026-03-15")
        assert out["status"] == "auth_required"
        assert out["product"] == "watermask_daily"

    def test_check_data_availability_aggregates_products(self, monkeypatch):
        from src.services import cygnss
        svc = cygnss.CYGNSSService()

        call_count = {"n": 0}
        def fake_search(product, bbox, days_back, limit):
            call_count["n"] += 1
            return {
                "status": "success",
                "granules_found": 3,
                "granules": [{"time_start": "2026-03-15T00:00:00Z"}],
            }

        monkeypatch.setattr(svc, "search_granules", fake_search)
        monkeypatch.setattr(svc, "_get_earthdata_auth", lambda: ("u", "p"))

        out = svc.check_data_availability(bbox=(29, -2, 30, -1))
        assert out["status"] == "success"
        assert out["auth_configured"] is True
        # One call per product in _COLLECTIONS (4 products)
        assert call_count["n"] == 4
        assert "soil_moisture_9km" in out["products"]
        assert out["products"]["soil_moisture_9km"]["available"] is True


# ── tools.json integrity ──


class TestAlosCygnssToolsIntegrity:
    def test_all_5_tools_present(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        names = [t["function"]["name"] for t in tools]
        for expected in [
            "get_alos_l_band_stats",
            "get_alos_temporal_variation",
            "check_cygnss_availability",
            "get_cygnss_soil_moisture",
            "get_cygnss_watermask",
        ]:
            assert expected in names, f"Tool {expected} missing from tools.json"

    def test_alos_tools_require_bbox(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        for name in ("get_alos_l_band_stats", "get_alos_temporal_variation"):
            t = next(x for x in tools if x["function"]["name"] == name)
            assert "bbox" in t["function"]["parameters"]["required"]

    def test_cygnss_soil_moisture_requires_lat_lon(self):
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        t = next(x for x in tools if x["function"]["name"] == "get_cygnss_soil_moisture")
        req = t["function"]["parameters"]["required"]
        assert "lat" in req
        assert "lon" in req

    def test_check_cygnss_availability_has_no_required(self):
        """Availability check should work for default Rwanda bbox without args."""
        import json
        import pathlib
        tools_path = pathlib.Path(__file__).parent.parent / "geoprocessing" / "tools.json"
        with open(tools_path) as f:
            tools = json.load(f)
        t = next(x for x in tools if x["function"]["name"] == "check_cygnss_availability")
        assert t["function"]["parameters"]["required"] == []

    def test_dispatch_blocks_wired(self):
        """Each new tool name must appear as an elif branch in message_routes.py."""
        import pathlib
        routes_path = pathlib.Path(__file__).parent.parent / "routes" / "message_routes.py"
        src = routes_path.read_text()
        for name in [
            "get_alos_l_band_stats",
            "get_alos_temporal_variation",
            "check_cygnss_availability",
            "get_cygnss_soil_moisture",
            "get_cygnss_watermask",
        ]:
            assert f'function_name == "{name}"' in src, f"dispatch missing for {name}"

    def test_system_prompt_capabilities_updated(self):
        import pathlib
        sp_path = pathlib.Path(__file__).parent.parent / "dependencies" / "system_prompt.py"
        src = sp_path.read_text()
        assert "get_alos_l_band_stats" in src
        assert "check_cygnss_availability" in src
        assert "get_cygnss_soil_moisture" in src
        assert "PALSAR" in src
        assert "CYGNSS" in src
