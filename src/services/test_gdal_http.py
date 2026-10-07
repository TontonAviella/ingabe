"""Remote reads in Sage tool paths give up on a server that never answers (2026-10-07).

GDAL and pystac-client set no HTTP timeout by default. Each test points the real library at a local
server that accepts the connection and then says nothing: without the limits these reads hang until
pytest's own timeout kills the test.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest

from src.services import sentinel1_service, stac_service, wapor_service
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

SHORT = {**GDAL_HTTP_TIMEOUTS, "GDAL_HTTP_TIMEOUT": "2"}


@pytest.fixture
def silent_server() -> Iterator[str]:
    """Base URL of a server that accepts connections and never sends a byte."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    sock.settimeout(0.2)
    held: list[socket.socket] = []
    stop = threading.Event()

    def accept() -> None:
        while not stop.is_set():
            try:
                held.append(sock.accept()[0])
            except OSError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    stop.set()
    thread.join()
    for conn in held:
        conn.close()
    sock.close()


def test_the_limits_are_set():
    assert {k: wapor_service.GDAL_COG_ENV[k] for k in GDAL_HTTP_TIMEOUTS} == GDAL_HTTP_TIMEOUTS
    assert stac_service.STAC_HTTP_TIMEOUT is not None


def test_a_wapor_point_read_gives_up(silent_server, monkeypatch):
    monkeypatch.setattr(wapor_service, "GDAL_COG_ENV", {**wapor_service.GDAL_COG_ENV, **SHORT})
    started = time.monotonic()
    value = wapor_service._read_point(f"{silent_server}/WAPOR-3.L2-AETI-D.2026-09-D1.tif", -1.9, 30.1, 0.1, 0.0)
    assert value is None
    assert time.monotonic() - started < 20


def test_a_sentinel1_window_read_gives_up(silent_server, monkeypatch):
    monkeypatch.setattr(sentinel1_service, "GDAL_HTTP_TIMEOUTS", SHORT)
    monkeypatch.setattr(sentinel1_service, "_sign_href", lambda href: href)
    started = time.monotonic()
    assert sentinel1_service._read_band_window(f"{silent_server}/vv.tif", (30.0, -2.0, 30.1, -1.9)) is None
    assert time.monotonic() - started < 20


def test_a_stac_catalog_that_never_answers_is_given_up(silent_server, monkeypatch):
    monkeypatch.setitem(stac_service.STAC_CATALOGS, "silent", silent_server)
    monkeypatch.setattr(stac_service, "STAC_HTTP_TIMEOUT", (2, 2))
    started = time.monotonic()
    svc = stac_service.STACService("silent")
    assert svc._pystac_client is None  # opening timed out; the service falls back to plain HTTP
    assert time.monotonic() - started < 20  # 3 tries of 2 s (STAC_HTTP_RETRIES)
