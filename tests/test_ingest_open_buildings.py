from __future__ import annotations

import json
import urllib.error

from scripts.ingest_open_buildings import _iter_tile_features
from src.services.open_buildings import bbox_geometry


def test_iter_tile_features_continues_after_download_failure(
    monkeypatch, capsys
) -> None:
    def fail_urlopen(*_args, **_kwargs):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(
        "scripts.ingest_open_buildings.urllib.request.urlopen",
        fail_urlopen,
    )

    features = list(
        _iter_tile_features(
            "https://example.test/open-buildings.csv.gz",
            bbox_geometry([30.0, -2.0, 30.1, -1.9]),
            0.75,
        )
    )

    assert features == []
    err = capsys.readouterr().err.strip()
    assert json.loads(err)["status"] == "tile_download_failed"
