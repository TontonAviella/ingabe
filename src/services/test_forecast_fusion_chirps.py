"""CHIRPS daily reads: the final product first, the preliminary one for recent days."""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

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
