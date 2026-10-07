"""CHIRPS daily reads: the final product first, the preliminary one for recent days."""

from __future__ import annotations

import io
import os
import threading
import time
import urllib.error
from unittest.mock import patch

import numpy as np
import pytest
from cachetools import TTLCache

from src.services import forecast_fusion as ff


def _http_404(url: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, 404, "Not Found", hdrs=None, fp=None)  # type: ignore[arg-type]


def test_final_product_is_used_when_published():
    with patch.object(ff, "_chirps_pixel", return_value=3.2) as pixel:
        assert ff._fetch_chirps_one(-1.9, 30.1, "2026-08-31") == ("2026-08-31", 3.2, False)
    assert pixel.call_args.args[0].startswith(ff._CHIRPS_BASE)


def test_preliminary_product_fills_days_the_final_one_lacks():
    def pixel(url, lat, lon):
        if url.startswith(ff._CHIRPS_BASE):
            raise _http_404(url)
        return 7.5
    with patch.object(ff, "_chirps_pixel", side_effect=pixel):
        assert ff._fetch_chirps_one(-1.9, 30.1, "2026-09-20") == ("2026-09-20", 7.5, True)


def test_a_day_neither_product_has_is_missing_not_zero():
    with patch.object(ff, "_chirps_pixel", side_effect=lambda url, *_: (_ for _ in ()).throw(_http_404(url))):
        assert ff._fetch_chirps_one(-1.9, 30.1, "2026-10-03") == ("2026-10-03", None, False)


def test_other_errors_do_not_fall_back():
    with patch.object(ff, "_chirps_pixel", side_effect=TimeoutError()) as pixel:
        assert ff._fetch_chirps_one(-1.9, 30.1, "2026-08-31") == ("2026-08-31", None, False)
    assert pixel.call_count == 1


def test_fetch_chirps_daily_reports_preliminary_days():
    answers = {"2026-08-31": ("2026-08-31", 1.0, False), "2026-09-20": ("2026-09-20", 2.0, True),
               "2026-10-03": ("2026-10-03", None, False)}
    with patch("rasterio.open"), patch.object(ff, "_fetch_chirps_one", side_effect=lambda lat, lon, d: answers[d]):
        values, prelim = ff.fetch_chirps_daily(-1.9, 30.1, list(answers))
    assert values == {"2026-08-31": 1.0, "2026-09-20": 2.0, "2026-10-03": None}
    assert prelim == {"2026-09-20"}


# ---------------------------------------------------------------------------
# The kept Rwanda part of each day (2026-10-07: 61 s of a 384 s report went to
# downloading CHIRPS files a report reads one pixel from).
# ---------------------------------------------------------------------------

FINAL = f"{ff._CHIRPS_BASE}/2026/chirps-v2.0.2026.08.31.tif.gz"
PRELIM = f"{ff._CHIRPS_PRELIM_BASE}/2026/chirps-v2.0.2026.09.30.tif.gz"
NODATA_AT = (-1.925, 29.925)  # (lat, lon) of a pixel set to CHIRPS nodata below


def _synthetic_day() -> bytes:
    """A GeoTIFF on the CHIRPS 0.05 deg grid over 27-33 E, 0-4 S, its values changing pixel to pixel."""
    from rasterio.io import MemoryFile
    from rasterio.transform import from_origin

    values = (np.arange(80 * 120, dtype=np.float32).reshape(80, 120) % 997) / 10.0
    transform = from_origin(27.0, 0.0, 0.05, 0.05)
    with MemoryFile() as mem:
        with mem.open(driver="GTiff", height=80, width=120, count=1, dtype="float32",
                      crs="EPSG:4326", transform=transform) as dst:
            row, col = dst.index(NODATA_AT[1], NODATA_AT[0])
            values[row, col] = -9999.0
            dst.write(values, 1)
        return mem.read()


def _read_whole_file(tif: bytes, lat: float, lon: float):
    """What _chirps_pixel returned before the cache: the pixel read from the whole file."""
    import rasterio
    with rasterio.open(io.BytesIO(tif)) as src:
        row, col = src.index(lon, lat)
        val = float(src.read(1)[row, col])
    return None if val < -9000 else round(max(0.0, val), 1)


@pytest.fixture
def chirps_server(tmp_path, monkeypatch):
    """CHIRPS downloads served from memory and counted; a fresh cache directory and 404 memory."""
    tif = _synthetic_day()
    calls: list[str] = []
    errors: dict[str, urllib.error.HTTPError] = {}

    def download(url):
        calls.append(url)
        time.sleep(0.05)
        if url in errors:
            raise errors[url]
        return tif

    monkeypatch.setattr(ff, "_CHIRPS_CACHE_DIR", tmp_path)
    monkeypatch.setattr(ff, "_chirps_unpublished", TTLCache(maxsize=64, ttl=3600))
    monkeypatch.setattr(ff, "_chirps_download", download)
    return tif, calls, errors


def test_a_kept_day_answers_any_place_in_rwanda_without_downloading_again(chirps_server):
    tif, calls, _ = chirps_server
    places = [(-1.95, 30.06), (-2.6, 29.74), (-1.3, 30.4), (-1.5, 29.6), (-2.0, 30.0), (-2.05, 30.15)]
    got = [ff._chirps_pixel(FINAL, lat, lon) for lat, lon in places]
    assert got == [_read_whole_file(tif, lat, lon) for lat, lon in places]
    assert calls == [FINAL]


def test_kept_values_are_the_file_values_everywhere_in_rwanda(chirps_server):
    """Same pixel and value as reading the whole file, on pixel edges too."""
    tif, calls, _ = chirps_server
    rng = np.random.default_rng(7)
    places = [(float(lat), float(lon)) for lat, lon in zip(rng.uniform(-2.9, -1.0, 300), rng.uniform(28.8, 31.0, 300))]
    places += [(-1.0 - 0.05 * i, 28.85 + 0.05 * j) for i in range(0, 38, 3) for j in range(0, 44, 3)]
    for lat, lon in places:
        assert ff._chirps_pixel(FINAL, lat, lon) == _read_whole_file(tif, lat, lon), (lat, lon)
    assert len(calls) == 1


def test_nodata_stays_missing_not_zero(chirps_server):
    assert ff._chirps_pixel(FINAL, *NODATA_AT) is None
    assert ff._chirps_pixel(FINAL, *NODATA_AT) is None  # from the kept part too


def test_a_place_outside_rwanda_reads_the_file_itself(chirps_server):
    tif, calls, _ = chirps_server
    ff._chirps_pixel(FINAL, -1.95, 30.06)  # keeps the day
    assert ff._chirps_pixel(FINAL, -1.5, 27.6) == _read_whole_file(tif, -1.5, 27.6)
    assert calls == [FINAL, FINAL]


def test_a_day_not_published_is_asked_for_again_only_after_a_while(chirps_server, monkeypatch):
    _, calls, errors = chirps_server
    now = [0.0]
    monkeypatch.setattr(ff, "_chirps_unpublished", TTLCache(maxsize=64, ttl=3 * 3600, timer=lambda: now[0]))
    errors[FINAL] = _http_404(FINAL)
    for _ in range(3):
        with pytest.raises(urllib.error.HTTPError) as e:
            ff._chirps_pixel(FINAL, -1.95, 30.06)
        assert e.value.code == 404
    assert calls == [FINAL]
    now[0] = 3 * 3600 + 1
    del errors[FINAL]
    assert ff._chirps_pixel(FINAL, -1.95, 30.06) is not None
    assert calls == [FINAL, FINAL]


def test_other_errors_are_not_remembered(chirps_server):
    _, calls, errors = chirps_server
    errors[FINAL] = urllib.error.HTTPError(FINAL, 429, "Too Many Requests", hdrs=None, fp=None)  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        ff._chirps_pixel(FINAL, -1.95, 30.06)
    del errors[FINAL]
    assert ff._chirps_pixel(FINAL, -1.95, 30.06) is not None
    assert calls == [FINAL, FINAL]


def test_preliminary_days_are_read_again_after_a_week_final_ones_are_not(chirps_server):
    _, calls, _ = chirps_server
    for url in (FINAL, PRELIM):
        ff._chirps_pixel(url, -1.95, 30.06)
        old = time.time() - ff._CHIRPS_PRELIM_MAX_AGE_S - 60
        os.utime(ff._chirps_cache_path(url), (old, old))
        ff._chirps_pixel(url, -1.95, 30.06)
    assert calls == [FINAL, PRELIM, PRELIM]


def test_an_unreadable_kept_file_is_downloaded_again(chirps_server):
    _, calls, _ = chirps_server
    ff._chirps_pixel(FINAL, -1.95, 30.06)
    ff._chirps_cache_path(FINAL).write_bytes(b"not a numpy file")
    assert ff._chirps_pixel(FINAL, -1.95, 30.06) is not None
    assert calls == [FINAL, FINAL]


def test_parallel_reads_of_one_file_download_it_once(chirps_server):
    _, calls, _ = chirps_server
    got: list = []
    threads = [threading.Thread(target=lambda: got.append(ff._chirps_pixel(FINAL, -1.95, 30.06))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(got)) == 1 and got[0] is not None
    assert calls == [FINAL]


def test_parallel_readers_of_a_day_not_published_ask_once(chirps_server):
    _, calls, errors = chirps_server
    errors[FINAL] = _http_404(FINAL)
    codes: list = []

    def read():
        try:
            ff._chirps_pixel(FINAL, -1.95, 30.06)
        except urllib.error.HTTPError as e:
            codes.append(e.code)

    threads = [threading.Thread(target=read) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert codes == [404] * 8
    assert calls == [FINAL]


def test_days_not_read_in_time_are_missing_not_dry():
    slow = {"2026-09-02"}

    def one(lat, lon, d):
        if d in slow:
            time.sleep(3)
        return d, 4.0, False

    started = time.monotonic()
    with patch.object(ff, "_fetch_chirps_one", side_effect=one):
        values, _ = ff.fetch_chirps_daily(-1.9, 30.1, ["2026-09-01", "2026-09-02", "2026-09-03"], timeout_s=0.5)
    assert time.monotonic() - started < 2
    assert values == {"2026-09-01": 4.0, "2026-09-02": None, "2026-09-03": 4.0}
