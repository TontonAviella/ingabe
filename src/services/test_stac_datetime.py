"""STAC datetime ranges go out in RFC 3339 (2026-10-07).

Earth Search and CDSE answer a bare-date range ("2026-09-07/2026-10-07") with 400 Bad Request.
`stac_datetime_interval` is checked against pystac-client itself, so a raw HTTP search asks for
the same scenes as a pystac-client search of the same range.
"""

from __future__ import annotations

import pytest

from src.services.stac_service import stac_datetime_interval

RANGES = [
    "2026-09-07/2026-10-07",
    "2026-09-07",
    "2026-09",
    "2024-02",
    "2026",
    "2026-09-07/..",
    "../2026-10-07",
    "/2026-10-07",
    "2026-09-07T05:00:00Z/2026-10-07T00:00:00Z",
    "2026-09-07T05:00:00/2026-10-07",
    "2026-09-07T05:00:00+02:00/2026-10-07",
    "2026-09-07T05:00:00.123Z",
]


@pytest.mark.parametrize("value", RANGES)
def test_same_interval_as_pystac_client(value):
    item_search = pytest.importorskip("pystac_client.item_search")
    expected = item_search.ItemSearch("http://127.0.0.1:9/unused", datetime=value).get_parameters()["datetime"]
    assert stac_datetime_interval(value) == expected


@pytest.mark.parametrize(
    "value", ["yesterday", "2026-02-30", "2026-13", "2026-09-07T05:00:00+0200", "2026-01-01/2026-02-01/2026-03-01"]
)
def test_rejects_what_pystac_client_rejects(value):
    with pytest.raises(ValueError):
        stac_datetime_interval(value)


@pytest.mark.parametrize(
    ("date_from", "date_to"), [("2026-09-07", "2026-10-07"), ("2024-02-29", "2024-02-29"), ("2025-12-31", "2026-01-01")]
)
def test_deafrica_day_ranges_are_unchanged(date_from, date_to):
    # deafrica_stac built this string itself before it used stac_datetime_interval.
    assert stac_datetime_interval(f"{date_from}/{date_to}") == f"{date_from}T00:00:00Z/{date_to}T23:59:59Z"
