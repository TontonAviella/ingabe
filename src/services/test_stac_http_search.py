"""The raw HTTP STAC search sends a request Earth Search accepts (2026-10-07).

When pystac-client could not open the catalog, `STACService._search_http` posted the caller's
bare-date range ("2026-09-07/2026-10-07"), Earth Search answered 400 Bad Request, and every
search through the fallback found nothing. These tests send the real request through `requests`
to a local server that behaves like Earth Search: its landing page fails, so pystac-client cannot
open it; its /search rejects a datetime Earth Search rejects, with the same 400 body; otherwise it
answers with a response recorded from Earth Search.

Recorded on 2026-10-07 into test_fixtures/stac/earth_search_s2_rwanda.json: POST
https://earth-search.aws.element84.com/v1/search with collections ["sentinel-2-l2a"], the Rwanda
bbox, datetime "2026-09-07T00:00:00Z/2026-10-07T23:59:59Z", limit 2 and
query {"eo:cloud_cover": {"lt": 20}}. Earth Search's datetime rule, checked against it the same
day: each end is an RFC 3339 date-time with a zone (Z or +HH:MM, optional fraction, T or t) or
open (".." or empty), and at least one end is closed. Bare dates, date-times without a zone and
"+0200" offsets got the "does not match RFC3339" 400.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
import requests

from src.services import stac_service
from src.services.stac_service import RWANDA_BBOX, STACService

RECORDED = Path(__file__).parent.parent.parent / "test_fixtures" / "stac" / "earth_search_s2_rwanda.json"

NOT_RFC3339 = {"code": "BadRequest", "description": "datetime value is invalid, does not match RFC3339 format"}
BOTH_ENDS_OPEN = {
    "code": "BadRequest",
    "description": "datetime value is invalid, at least one end of the interval must be closed",
}
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[-+]\d{2}:\d{2})$")


def _earth_search_datetime_error(value: str) -> Optional[Dict[str, str]]:
    ends = value.split("/")
    if len(ends) > 2 or not all(end in ("", "..") or _RFC3339.match(end) for end in ends):
        return NOT_RFC3339
    if all(end in ("", "..") for end in ends):
        return BOTH_ENDS_OPEN
    return None


@pytest.fixture
def earth_search() -> Iterator[Tuple[str, List[Dict[str, Any]]]]:
    """Base URL of a local Earth Search, and the search bodies it received."""
    recorded = RECORDED.read_bytes()
    received: List[Dict[str, Any]] = []

    class EarthSearch(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/geo+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(404, b"{}")

        def do_POST(self):
            if self.path != "/v1/search":
                self._send(404, b"{}")
                return
            search = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(search)
            error = _earth_search_datetime_error(search["datetime"]) if "datetime" in search else None
            if error:
                self._send(400, json.dumps(error).encode())
            else:
                self._send(200, recorded)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), EarthSearch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1", received
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.fixture
def service(earth_search, monkeypatch) -> STACService:
    """An Earth Search service on the local server, through the raw HTTP fallback."""
    monkeypatch.setitem(stac_service.STAC_CATALOGS, "earth_search", earth_search[0])
    service = STACService("earth_search")
    assert service._pystac_client is None  # its landing page fails, as pystac-client's open did
    return service


@pytest.mark.parametrize(
    "datetime_range",
    [
        "2026-09-07/2026-10-07",
        "2026-09-07T00:00:00/2026-10-07T23:59:59",
        "2026-09-07T00:00:00+0200/..",
    ],
)
def test_the_local_server_rejects_what_earth_search_rejected(earth_search, datetime_range):
    base_url, _ = earth_search
    search = {"collections": ["sentinel-2-l2a"], "bbox": RWANDA_BBOX, "datetime": datetime_range, "limit": 2}
    response = requests.post(f"{base_url}/search", json=search, timeout=30)
    assert response.status_code == 400
    assert response.json() == NOT_RFC3339


def test_a_fallback_search_returns_the_scenes_earth_search_found(service, earth_search):
    _, received = earth_search
    recorded = json.loads(RECORDED.read_text())

    result = service.search_imagery(datetime_range="2026-09-07/2026-10-07", max_cloud_cover=20, limit=2)

    assert received == [
        {
            "collections": ["sentinel-2-l2a"],
            "bbox": RWANDA_BBOX,
            "datetime": "2026-09-07T00:00:00Z/2026-10-07T23:59:59Z",
            "limit": 2,
            "query": {"eo:cloud_cover": {"lt": 20}},
        }
    ]
    assert "error" not in result
    assert result["matched"] == 2
    for item, feature in zip(result["items"], recorded["features"], strict=True):
        assert item["id"] == feature["id"]
        assert item["cloud_cover"] == feature["properties"]["eo:cloud_cover"]
        assert item["cloud_cover"] < 20
        # NDVI downstream reads red and NIR from these hrefs.
        assert STACService._resolve_band_keys(item["assets"]) == ("red", "nir")
        assert item["assets"]["red"]["href"] == feature["assets"]["red"]["href"]
        assert item["assets"]["nir"]["href"] == feature["assets"]["nir"]["href"]


@pytest.mark.parametrize(
    "datetime_range",
    [None, "2026-09-07", "2026-09", "2026-09-07/..", "../2026-10-07", "2026-09-07T05:00:00+02:00/2026-10-07"],
)
def test_every_range_callers_pass_is_accepted(service, earth_search, datetime_range):
    result = service.search_imagery(datetime_range=datetime_range)
    assert "error" not in result, result
    assert result["matched"] == 2


def test_an_unreadable_range_is_an_error_and_no_request(service, earth_search):
    _, received = earth_search
    result = service.search_imagery(datetime_range="last month")
    assert "invalid STAC datetime" in result["error"]
    assert received == []
