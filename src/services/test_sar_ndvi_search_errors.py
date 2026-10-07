"""A failed Sentinel-2 search is reported as a failure, not as "no data" (2026-10-07).

SAR->NDVI training read `compute_admin_ndvi(...).get("observations", [])`, so a search that failed
(every Earth Search request was a 400 that day) was logged as "Insufficient S2 observations: 0",
and predict_ndvi fell back to its empirical guess without saying why. The satellite display and
spectral-index tools said "No Sentinel-2 scenes found", and the NDVI-stats fallback "found no
cloud-free Sentinel-2 scenes" for the same failure. (The drought satellite fallback, which said
"insufficient cloud-free Sentinel-2 scenes", was removed instead.)

The failure here is real: STACService searches a local port nothing listens on, so the error dict
is the one STACService itself returns. Earth Search's empty answer is copied from a real response
(a bbox in the open ocean, 2026-10-07).
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict
from unittest.mock import patch

import pytest

from src.services import sar_ndvi, sentinel1_service, stac_service
from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext
from src.services.sar_ndvi import SARNDVIPredictor
from src.tools.display_layer import DisplaySatelliteLayerArgs, display_satellite_layer
from src.tools.pyd import IngabeToolCallMetaArgs
from src.tools.spectral_index import ComputeSpectralIndexArgs, compute_spectral_index

BBOX = (29.3, -2.0, 29.4, -1.9)
EMPTY_SEARCH = {
    "type": "FeatureCollection", "stac_version": "1.0.0", "stac_extensions": [],
    "context": {"limit": 20, "matched": 0, "returned": 0}, "numberMatched": 0, "numberReturned": 0, "features": [],
}


def _use_catalog(monkeypatch, url: str) -> None:
    """Point Earth Search at `url`, through STACService's raw HTTP search."""
    monkeypatch.setattr(stac_service, "_PYSTAC_CLIENT_AVAILABLE", False)
    monkeypatch.setitem(stac_service.STAC_CATALOGS, "earth_search", url)
    monkeypatch.setattr(stac_service, "_stac_service", None)  # get_stac_service builds a fresh one


@pytest.fixture
def dead_catalog(monkeypatch) -> str:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()  # nothing listens: the connection is refused, as in an outage
    _use_catalog(monkeypatch, f"http://127.0.0.1:{port}/v1")
    return f"port={port}"  # how requests names the refused connection


@pytest.fixture
def empty_catalog(monkeypatch) -> Iterator[None]:
    body = json.dumps(EMPTY_SEARCH).encode()

    class Search(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/geo+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Search)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _use_catalog(monkeypatch, f"http://127.0.0.1:{server.server_port}/v1")
    yield
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def sentinel1(monkeypatch) -> None:
    """Six Sentinel-1 scenes, in the shape Sentinel1Service.get_time_series returns."""
    series: Dict[str, Any] = {
        "status": "success",
        "dates": [f"2026-09-{day:02d}T16:13:37.123456Z" for day in (2, 8, 14, 20, 26, 30)],
        "vv_means": [-10.5, -10.2, -10.8, -10.1, -10.4, -10.6],
        "vv_stds": [1.1, 1.0, 1.2, 1.1, 1.0, 1.1],
        "vh_means": [-17.2, -17.0, -17.5, -16.9, -17.1, -17.3],
        "vh_stds": [1.5, 1.4, 1.6, 1.5, 1.4, 1.5],
        "scene_count": 6,
    }

    class Sentinel1:
        def get_time_series(self, bbox, date_range, limit=20):
            return series

    monkeypatch.setattr(sentinel1_service, "get_sentinel1_service", Sentinel1)


def test_training_says_the_sentinel2_search_failed(dead_catalog, sentinel1):
    with patch.object(sar_ndvi, "logger") as log:
        result = SARNDVIPredictor().train_model(BBOX)

    assert result["status"] == "error"
    assert "Sentinel-2 NDVI search failed (earth_search)" in result["error"]
    assert dead_catalog in result["error"]
    log.warning.assert_called_once()
    assert "Sentinel-2 NDVI search failed" in str(log.warning.call_args)


def test_too_few_scenes_is_not_reported_as_a_failure(empty_catalog, sentinel1):
    result = SARNDVIPredictor().train_model(BBOX)
    assert result["error"] == "Model not trained: 0 Sentinel-2 NDVI observations in the last 180 days, need at least 5"


def test_the_empirical_fallback_says_why_the_model_is_not_used(dead_catalog, sentinel1):
    result = SARNDVIPredictor().predict_ndvi(BBOX)
    assert result["method"] == "empirical_cross_pol_ratio"
    assert "Sentinel-2 NDVI search failed" in result["model_unavailable_reason"]


META = IngabeToolCallMetaArgs(
    user_uuid="user-test", conversation_id=1, map_id="MTESTAAAAAAA", project_id="PTESTBBBBBBB", session=None
)


async def test_display_satellite_layer_reports_a_failed_search(dead_catalog):
    args = DisplaySatelliteLayerArgs(bbox="29.3,-2.0,29.4,-1.9", date_from="2026-09-07", date_to="2026-10-07", layer_name="x")
    result = await display_satellite_layer(args, META)
    assert result["status"] == "error"
    assert result["error"].startswith("Satellite imagery search failed:")
    assert dead_catalog in result["error"]


async def test_compute_spectral_index_reports_a_failed_search(dead_catalog):
    args = ComputeSpectralIndexArgs(
        bbox="29.3,-2.0,29.4,-1.9", index="ndvi", date_from="2026-09-07", date_to="2026-10-07", layer_name="x"
    )
    result = await compute_spectral_index(args, META)
    assert result["status"] == "error"
    assert result["error"].startswith("Satellite imagery search failed:")
    assert dead_catalog in result["error"]


GASABO = {"district": "Gasabo", "bbox_west": 29.3, "bbox_south": -2.0, "bbox_east": 29.4, "bbox_north": -1.9}


class Conn:
    """Answers the handlers' SQL as an empty NDVI and drought cache would, with no geometry for Digital
    Earth Africa and `boundaries` for the district bbox query (rows read by key, like asyncpg Records)."""

    def __init__(self, boundaries):
        self.boundaries = boundaries

    async def fetch(self, sql, *params):
        if "FROM ndvi_field_cache" in sql or "FROM drought_cache" in sql or "ST_AsGeoJSON" in sql:
            return []
        if "bbox_west" in sql:
            return self.boundaries
        raise AssertionError(f"unexpected query: {sql}")

    def transaction(self):
        return contextlib.nullcontext()


async def _call(tool: str, boundaries, **arguments) -> Dict[str, Any]:
    ctx = LegacyToolContext(
        user_id="user-test", partner_id="partner-test", conversation_id=1, map_id="MTESTAAAAAAA",
        project_id="PTESTBBBBBBB", conn=Conn(boundaries), arguments=arguments,
    )
    return await LEGACY_HANDLERS[tool](ctx)


async def test_drought_with_an_empty_cache_says_there_is_no_assessment(monkeypatch):
    def no_satellite_fallback(*args, **kwargs):
        raise AssertionError("drought must not be computed from a few satellite scenes")

    monkeypatch.setattr(stac_service, "get_stac_service", no_satellite_fallback)
    result = await _call("get_drought_status", [GASABO], district="Gasabo")
    assert result["status"] == "success"
    assert result["districts"] == []
    assert "Do NOT report a drought status" in result["note"]


async def test_ndvi_stats_fallback_reports_a_failed_search(dead_catalog):
    result = await _call("get_ndvi_stats", [GASABO], district="Gasabo")
    assert result["status"] == "error"
    assert result["error"].startswith("Sentinel-2 search failed for Gasabo:")
    assert dead_catalog in result["error"]


async def test_ndvi_stats_without_scenes_still_says_no_data(empty_catalog):
    result = await _call("get_ndvi_stats", [GASABO], district="Gasabo")
    assert result["status"] == "success"
    assert result["ndvi_stats"] == []
    assert result["message"].startswith("No NDVI data available.")
